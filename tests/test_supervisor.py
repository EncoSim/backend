"""The supervisor: process handling, readiness, restarts, and the specs of the real stack."""

import json
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _pytest.logging import LogCaptureFixture

from encosim_server import PROJECT_ROOT
from encosim_server.celery import create_celery
from encosim_server.core.settings import Settings
from encosim_server.logs import service_log_file
from encosim_server.supervisor import (
    ServiceFailed,
    ServiceSpec,
    Supervisor,
    stack_specs,
)
from tests.helpers import is_running

pytestmark = pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")

PY = sys.executable
FOREVER = "import time; time.sleep(60)"


def spec(name: str, code: str = FOREVER, **kwargs: Any) -> ServiceSpec:
    kwargs.setdefault("ready", lambda: True)
    kwargs.setdefault("ready_timeout", 10)
    kwargs.setdefault("stop_grace", 2)
    return ServiceSpec(name, argv=[PY, "-c", code], cwd=PROJECT_ROOT, **kwargs)


def pid_of(supervisor: Supervisor, name: str) -> int:
    proc = supervisor[name].proc
    assert proc
    return proc.pid


def read_json(path: Path):
    return json.loads(path.read_text())  # raises while the file is missing or half written


# --- construction ---------------------------------------------------------------------------------


def test_service_names_must_be_unique(settings: Settings):
    with pytest.raises(ValueError, match="unique"):
        Supervisor(settings, [spec("a"), spec("a")])


def test_dependencies_must_be_listed_first(settings: Settings):
    with pytest.raises(ValueError, match="listed first"):
        Supervisor(settings, [spec("b", depends_on=("a",)), spec("a")])


def test_the_env_file_is_private_and_removed_afterwards(settings: Settings):
    with Supervisor(settings, []) as supervisor:
        env_file = supervisor.env_file
        assert env_file and env_file.is_file()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    assert not env_file.exists()


def test_services_cannot_start_before_the_supervisor_is_open(settings: Settings):
    supervisor = Supervisor(settings, [spec("a")])

    with pytest.raises(RuntimeError, match="context manager"):
        supervisor.ensure("a")


# --- what the children see ------------------------------------------------------------------------


def test_children_get_exactly_the_supervisors_settings(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ENCOSIM_PORT", "1234")  # must not leak into the children
    out = tmp_path / "seen.json"
    code = textwrap.dedent(
        f"""
        import json, pathlib, time
        from encosim_server.core.settings import Settings
        s = Settings.load()
        data = [str(s.redis_conn), str(s.logs_dir), s.celery_hard_timeout.total_seconds(), s.port]
        pathlib.Path({str(out)!r}).write_text(json.dumps(data))
        time.sleep(60)
        """
    )

    with Supervisor(settings, [spec("probe", code, ready=lambda: bool(read_json(out)))]) as sup:
        sup.start()

    assert read_json(out) == [
        str(settings.redis_conn),
        str(settings.logs_dir),
        settings.celery_hard_timeout.total_seconds(),
        settings.port,
    ]


def test_output_is_tagged_and_copied_to_the_log_file(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    log = tmp_path / "logs" / "redis" / "redis.log"
    code = "import time; print('hello', flush=True); print('world', flush=True); time.sleep(60)"
    ready = lambda: log.exists() and "world" in log.read_text()  # noqa: E731

    with Supervisor(settings, [spec("redis", code, tee_to=log, ready=ready)]) as supervisor:
        supervisor.start()
        tail = supervisor["redis"].output_tail()

    out = capsys.readouterr().out
    assert "| hello" in out and "| world" in out and "redis" in out
    assert log.read_text() == "hello\nworld\n"
    assert tail == "hello\nworld\n"


# --- start, stop, restart -------------------------------------------------------------------------


def test_ensure_starts_dependencies_first_and_is_idempotent(settings: Settings):
    with Supervisor(settings, [spec("a"), spec("b", depends_on=("a",))]) as supervisor:
        service = supervisor.ensure("b")

        assert supervisor["a"].alive() and service.alive()
        assert supervisor.ensure("b") is service
        assert pid_of(supervisor, "b") == service.proc.pid  # type: ignore


def test_stopping_a_service_also_stops_what_depends_on_it(settings: Settings):
    with Supervisor(settings, [spec("a"), spec("b", depends_on=("a",)), spec("c")]) as sup:
        sup.start()

        sup.stop("a")

        assert not sup["a"].alive() and not sup["b"].alive()
        assert sup["c"].alive()


def test_restart_restarts_the_dependents_too(settings: Settings):
    with Supervisor(settings, [spec("a"), spec("b", depends_on=("a",)), spec("c")]) as sup:
        sup.start()
        before = {name: pid_of(sup, name) for name in "abc"}

        sup.restart("a")

        after = {name: pid_of(sup, name) for name in "abc"}
        assert after["a"] != before["a"] and after["b"] != before["b"]
        assert after["c"] == before["c"]
        assert sup["a"].alive() and sup["b"].alive()


def test_a_service_that_dies_during_startup_is_reported_with_its_output(settings: Settings):
    # The probe can never succeed, so the only way out of wait_ready is seeing the dead process.
    # Otherwise the SystemExit(3) races the ready probe (and always loses due to startup time)
    broken = spec("broken", "print('boom'); raise SystemExit(3)", ready=lambda: False)

    with Supervisor(settings, [broken]) as supervisor:
        with pytest.raises(ServiceFailed, match="code 3") as info:
            supervisor.ensure("broken")

        assert "boom" in str(info.value)
        assert not supervisor["broken"].alive()


def test_a_service_that_never_gets_ready_is_stopped(settings: Settings):
    stuck = spec("stuck", ready=lambda: False, ready_timeout=1)

    with Supervisor(settings, [stuck]) as supervisor:
        with pytest.raises(ServiceFailed, match="not ready"):
            supervisor.ensure("stuck")

        assert not supervisor["stuck"].alive()


# one process that ignores SIGTERM, whose parent exits on it (a stuck celery pool process)
def straggler_spec(pid_file: Path) -> ServiceSpec:
    child = (
        "import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    )
    return spec("leader", leader, ready=lambda: pid_file.read_text().isdigit(), stop_grace=1)


def test_stopping_also_kills_children_that_outlive_their_parent(settings: Settings, tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    with Supervisor(settings, [straggler_spec(pid_file)]) as supervisor:
        supervisor.start()
        pid = int(pid_file.read_text())  # written after SIGTERM was ignored
        try:
            supervisor.stop("leader")

            assert not is_running(pid)
        finally:
            if is_running(pid):
                os.kill(pid, signal.SIGKILL)


def test_stopping_twice_is_harmless(settings: Settings, caplog: LogCaptureFixture):
    with Supervisor(settings, [spec("a")]) as supervisor:
        supervisor.start()
        with caplog.at_level(logging.INFO):
            supervisor.stop_all()
            supervisor.stop_all()

        assert (
            len([r.getMessage() for r in caplog.records if r.getMessage().startswith("stopped a")])
            == 1
        )


# --- the development loop -------------------------------------------------------------------------


def interrupt_when(condition: Callable[[], bool], timeout: float = 30):
    """Send Ctrl+C to this process once `condition` holds (from a helper thread)."""

    def run():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if condition():
                    break
            except Exception:
                pass
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=run, daemon=True).start()


def counting(counter: Path, then: str) -> str:
    """Code that counts its starts in `counter` and then runs `then`."""
    return (
        "import pathlib, sys, time\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n" + then
    )


def count(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


def test_a_crashed_service_is_restarted(settings: Settings, tmp_path: Path):
    counter = tmp_path / "starts"
    code = counting(counter, "time.sleep(0.5 if n == 0 else 60)\nsys.exit(1 if n == 0 else 0)")
    supervisor = Supervisor(settings, [spec("flaky", code)])
    interrupt_when(lambda: count(counter) >= 2)

    with supervisor:
        exit_code = supervisor.run_forever()

    assert exit_code == 0
    assert count(counter) == 2


def test_a_service_that_keeps_crashing_stops_the_whole_stack(settings: Settings, tmp_path: Path):
    counter = tmp_path / "starts"
    always_crashes = spec("crashy", counting(counter, "time.sleep(0.3)"))
    healthy = spec("healthy")

    with Supervisor(settings, [healthy, always_crashes]) as supervisor:
        exit_code = supervisor.run_forever(max_crashes=2)

        assert exit_code == 1
        assert not supervisor["healthy"].alive()  # stopped with the rest
    assert count(counter) >= 3


def test_a_crash_restarts_the_dependents_as_well(settings: Settings, tmp_path: Path):
    counter = tmp_path / "dependent-starts"
    crash_once = tmp_path / "crashed"
    base = (
        "import pathlib, sys, time\n"
        f"flag = pathlib.Path({str(crash_once)!r})\n"
        "if not flag.exists():\n    flag.write_text('x'); time.sleep(0.5); sys.exit(1)\n"
        "time.sleep(60)"
    )
    specs = [
        spec("base", base),
        spec("dependent", counting(counter, FOREVER), depends_on=("base",)),
    ]
    interrupt_when(lambda: count(counter) >= 2)

    with Supervisor(settings, specs) as supervisor:
        assert supervisor.run_forever() == 0

    assert count(counter) == 2  # started again together with the service it depends on


def test_changed_files_restart_the_watching_service(settings: Settings, tmp_path: Path):
    pytest.importorskip("watchfiles")
    watched = tmp_path / "src"
    watched.mkdir()
    counter = tmp_path / "starts"
    service = spec("worker", counting(counter, FOREVER), watch=(watched,))

    def touch_when_started():
        deadline = time.monotonic() + 30
        while count(counter) < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1)  # let the watcher settle
        (watched / "changed.py").write_text("x = 1\n")

    threading.Thread(target=touch_when_started, daemon=True).start()
    interrupt_when(lambda: count(counter) >= 2)

    with Supervisor(settings, [service]) as supervisor:
        assert supervisor.run_forever() == 0

    assert count(counter) == 2


# --- the specs of the stack -----------------------------------------------------------------------


def test_stack_specs_describe_the_whole_stack(settings: Settings):
    specs = {s.name: s for s in stack_specs(settings, reload=True, include=["tests.celery_tasks"])}

    assert list(specs) == ["redis", "celery", "backend"]
    assert specs["celery"].depends_on == specs["backend"].depends_on == ("redis",)
    assert specs["celery"].watch  # celery reloads on changes, uvicorn does it itself
    assert not specs["backend"].watch and "--reload" in specs["backend"].argv
    assert "--include=tests.celery_tasks" in specs["celery"].argv
    assert specs["redis"].tee_to == service_log_file(settings.logs_dir, "redis")  # type: ignore


def test_without_reload_nothing_is_watched(settings: Settings):
    specs = stack_specs(settings, services=("celery", "backend"))

    assert all(not s.watch and not s.depends_on for s in specs)  # redis is not ours here
    assert "--reload" not in specs[1].argv


def test_unknown_services_are_rejected(settings: Settings):
    with pytest.raises(ValueError, match="unknown"):
        stack_specs(settings, services=("redis", "postgres"))


def test_redis_needs_its_binary(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("encosim_server.supervisor.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError, match="redis-server"):
        stack_specs(settings, services=("redis",))


def test_a_running_redis_is_never_replaced(settings: Settings):
    # the suite's own redis listens on settings.redis_conn
    with Supervisor(settings, stack_specs(settings, services=("redis",))) as supervisor:
        with pytest.raises(FileExistsError, match="already running"):
            supervisor.ensure("redis")


@pytest.fixture
def own_settings(settings: Settings):
    run_dir = Path(tempfile.mkdtemp(prefix="encosim-sup-", dir="/tmp"))  # short socket path
    yield settings.model_copy(
        update={"redis_conn": run_dir / "redis.sock", "logs_dir": run_dir / "logs"}
    )
    shutil.rmtree(run_dir, ignore_errors=True)


def test_a_stale_redis_socket_is_replaced(own_settings: Settings):
    own_settings.redis_conn.touch()  # left behind by a crashed redis

    with Supervisor(own_settings, stack_specs(own_settings, services=("redis",))) as supervisor:
        assert supervisor.ensure("redis").alive()


async def test_redis_and_celery_start_and_stop_together(own_settings: Settings):
    specs = stack_specs(own_settings, services=("redis", "celery"), cwd=PROJECT_ROOT)

    with Supervisor(own_settings, specs) as supervisor:
        supervisor.start()  # returns when both answer their probes

        assert supervisor["redis"].alive() and supervisor["celery"].alive()
        assert create_celery(own_settings).control.ping(timeout=1.0, limit=1)

    assert not supervisor["celery"].alive() and not supervisor["redis"].alive()
    assert not own_settings.redis_conn.exists()  # redis shut down cleanly: it removes its socket
    assert service_log_file(own_settings.logs_dir, "redis").stat().st_size > 0  # type: ignore
    assert service_log_file(own_settings.logs_dir, "celery").stat().st_size > 0  # type: ignore
