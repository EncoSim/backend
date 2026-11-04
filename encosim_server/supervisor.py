"""Starts, watches and stops the processes of a development stack (redis, celery, backend).

Used by `encosim dev` and by the test suite. Production runs under supervisord (see `bootstrap.py`).

* Every process is described once, as a `ServiceSpec`: its command, how to tell that it is ready,
  what it depends on and which files it reloads on. `stack_specs` builds the specs from `Settings`.
* The settings reach the children as a private JSON file generated from the supervisor's own
  `Settings` object, so a child always sees exactly what the supervisor sees (tests can inject
  settings like this).
* Use it as a context manager to start and stop services by hand (tests), or call `run_forever`
  for the development loop: restart on crash, restart on file changes, stop on Ctrl+C.
"""

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from typing import IO

import redis
from celery import Celery

from encosim_server.celery import create_celery
from encosim_server.core.configfile import ConfigFileWriter
from encosim_server.core.settings import CONFIG_FILE_VAR, Settings
from encosim_server.database import get_redis_kwargs
from encosim_server.logs import service_log_file, tagged_print

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
SERVICES = ("redis", "celery", "backend")


class SupervisorError(RuntimeError):
    pass


class ServiceFailed(SupervisorError):
    pass


@dataclass(frozen=True)
class ServiceSpec:
    name: str  # handle
    argv: Sequence[str]  # command + arguments
    ready: Callable[[], bool]  # test is process is ready
    depends_on: tuple[str, ...] = ()  # names of required processes
    watch: tuple[Path, ...] = ()  # restart the service (and its dependents) when files change
    tee_to: Path | None = None  # copy the output here (for programs that cannot log to a file)
    cwd: Path | None = None
    prepare: Callable[[], None] | None = None  # runs before every (re)start
    ready_timeout: float = 30.0
    stop_grace: float = 10.0


def child_environment(config_file: Path) -> dict[str, str]:
    """Keep external settings out; pass only the generated config-file path to the child."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENCOSIM_")}
    env[CONFIG_FILE_VAR] = str(config_file)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Service:
    """A restartable child process that leads its own process group."""

    TAIL_LINES = 200

    def __init__(self, spec: ServiceSpec, env: Mapping[str, str], echo: bool = True):
        self.spec = spec
        self.env = dict(env)
        self.echo = echo
        self.proc: subprocess.Popen[str] | None = None
        self.pgid: int | None = None
        self._tail: deque[str] = deque(maxlen=self.TAIL_LINES)
        self._thread: threading.Thread | None = None
        self._tee: IO[str] | None = None
        self._started = False

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.returncode

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def output_tail(self, lines: int = 40) -> str:
        return "".join(list(self._tail)[-lines:])

    def start(self):
        if self.spec.prepare:
            self.spec.prepare()
        if self.spec.tee_to:
            self.spec.tee_to.parent.mkdir(parents=True, exist_ok=True)
            self._tee = self.spec.tee_to.open("a", encoding="utf-8", buffering=1)
        self.proc = subprocess.Popen(
            list(self.spec.argv),
            cwd=self.spec.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own group: Ctrl+C only reaches the supervisor
        )
        self.pgid = self.proc.pid  # the leader of a new session also leads a new group
        self._thread = threading.Thread(
            target=self._pump, args=(self.proc, self._tee), daemon=True, name=f"pump-{self.name}"
        )
        self._thread.start()
        self._started = True
        logger.info(f"started {self.name} (pid {self.proc.pid})")

    def _pump(self, proc: subprocess.Popen[str], tee: IO[str] | None):
        assert proc.stdout
        for line in proc.stdout:
            self._tail.append(line)
            if self.echo:
                tagged_print(self.name, line)
            if tee:
                tee.write(line)

    def _signal(self, sig: int):
        # The group id was stored at start: it stays valid while any member lives, even after
        # the leader exited and was reaped (os.getpgid(pid) would fail by then).
        if self.pgid is None:
            return
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            pass

    def stop(self):
        if not self._started:  # never started, or already stopped
            return
        self._started = False

        if self.proc is None:
            return
        self._signal(signal.SIGTERM)
        try:
            self.proc.wait(self.spec.stop_grace)
        except subprocess.TimeoutExpired:
            logger.warning(f"{self.name} did not stop within {self.spec.stop_grace}s, killing it")
        self._signal(signal.SIGKILL)  # also catches members that ignore SIGTERM
        self.proc.wait()
        if self._thread:
            self._thread.join(3)
        if self._tee:
            self._tee.close()
            self._tee = None
        logger.info(f"stopped {self.name} (pid {self.proc.pid})")

    def wait_ready(self):
        deadline = time.monotonic() + self.spec.ready_timeout
        while True:
            if not self.alive():
                raise ServiceFailed(
                    f"{self.name} exited with code {self.returncode} before it was ready\n"
                    f"{self.output_tail()}"
                )
            try:
                if self.spec.ready():
                    return
            except Exception:
                pass  # still booting
            if time.monotonic() > deadline:
                raise ServiceFailed(
                    f"{self.name} was not ready after {self.spec.ready_timeout}s\n"
                    f"{self.output_tail()}"
                )
            time.sleep(0.1)


class Supervisor:
    """Supervise a stack of services (order of dependency: `[parent, child, grandchild, ...]`).

    Services inherit settings via temporary config files
    """
    def __init__(self, settings: Settings, specs: Sequence[ServiceSpec], echo: bool = True):
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("service names must be unique")
        for i, spec in enumerate(specs):
            for dependency in spec.depends_on:
                if dependency not in names[:i]:
                    raise ValueError(f"{spec.name} needs {dependency}, which must be listed first")
        self.settings = settings
        self.specs = {spec.name: spec for spec in specs}  # in start order
        self.echo = echo
        self._services: dict[str, Service] = {}
        self._config_file: Path | None = None
        self._requests: Queue[str] = Queue()  # services whose files changed
        self._lock = threading.RLock()

    def __getitem__(self, name: str) -> Service:
        return self._services[name]

    @property
    def config_file(self) -> Path | None:
        """The generated JSON file the children read (while the supervisor is open)."""
        return self._config_file

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        """Generate the resolved JSON configuration the children will read. Starts nothing."""
        fd, path = tempfile.mkstemp(prefix="encosim-", suffix=".json")  # private (0600)
        os.close(fd)
        self._config_file = Path(path)
        ConfigFileWriter(self._config_file, self.settings).save_settings()

    def close(self):
        self.stop_all()
        if self._config_file:
            self._config_file.unlink(missing_ok=True)
            self._config_file = None

    # --- starting and stopping ---------------------------------------------------------------

    def ensure(self, name: str) -> Service:
        """Start `name` (and what it depends on) unless it runs, and wait until it is ready."""
        with self._lock:
            if self._config_file is None:
                raise SupervisorError("use the supervisor as a context manager")
            spec = self.specs[name]
            for dependency in spec.depends_on:
                self.ensure(dependency)
            service = self._services.get(name)
            if service is None:
                argv = list(spec.argv)
                if name in ("celery", "backend"):
                    command = "worker" if name == "celery" else "serve"
                    try:
                        command_index = argv.index(command)
                    except ValueError as exc:
                        raise SupervisorError(
                            f"{name} service command does not contain {command!r}"
                        ) from exc
                    argv[command_index + 1 : command_index + 1] = [
                        "--config",
                        str(self._config_file),
                    ]
                    spec = replace(spec, argv=argv)
                service = self._services[name] = Service(
                    spec, child_environment(self._config_file), echo=self.echo
                )
            if not service.alive():
                try:
                    service.start()
                    service.wait_ready()
                except BaseException:
                    service.stop()
                    raise
            return service

    def start(self, *names: str):
        """Start the given services, or all of them."""
        for name in names or self.specs:
            self.ensure(name)

    def stop(self, name: str):
        """Stop `name` and everything that depends on it."""
        with self._lock:
            for dependent in reversed(self._with_dependents(name)):
                if dependent in self._services:
                    self._services[dependent].stop()

    def restart(self, name: str):
        """Restart `name` and everything that depends on it (they hold connections to it)."""
        with self._lock:
            group = [n for n in self._with_dependents(name) if n in self._services]
            for n in reversed(group):
                self._services[n].stop()
            for n in group:
                self.ensure(n)

    def stop_all(self):
        with self._lock:
            for name in reversed(list(self.specs)):
                if name in self._services:
                    self._services[name].stop()

    def _with_dependents(self, name: str) -> list[str]:
        group = {name}
        for spec in self.specs.values():  # dependencies are listed before their dependents
            if group & set(spec.depends_on):
                group.add(spec.name)
        return [n for n in self.specs if n in group]

    # --- the development loop ----------------------------------------------------------------

    def run_forever(self, *, max_crashes: int = 5, window: float = 60.0) -> int:
        """Start everything and keep it running until Ctrl+C. Returns the exit code.

        A service that dies is restarted together with its dependents. After `max_crashes`
        within `window` seconds a service with files to watch waits for the next change,
        any other gives up and stops the whole stack.
        """
        stop = threading.Event()
        previous = {
            sig: signal.signal(sig, lambda *_: stop.set())
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        watcher_stop = threading.Event()
        crashes: dict[str, deque[float]] = {name: deque() for name in self.specs}
        parked: set[str] = set()  # crashed too often, waiting for a file change

        def restart(name: str, *, crashed: bool):
            if crashed:
                now = time.monotonic()
                history = crashes[name]
                history.append(now)
                while history and now - history[0] > window:
                    history.popleft()
                if len(history) > max_crashes:
                    if not self.specs[name].watch:
                        raise ServiceFailed(f"{name} keeps crashing")
                    parked.add(name)
                    logger.error(f"{name} keeps crashing, waiting for file changes")
                    return
                stop.wait(1.0)  # do not spin
            try:
                self.restart(name)
            except ServiceFailed as exc:
                logger.error(str(exc))  # the next round sees it down again

        code = 0
        try:
            self._start_watchers(watcher_stop)
            self.start()
            logger.info("everything is up, press Ctrl+C to stop")
            while not stop.wait(0.5):
                for name in self._drain_requests():
                    parked.discard(name)
                    logger.info(f"files changed, restarting {name}")
                    restart(name, crashed=False)
                for name, service in list(self._services.items()):
                    if name not in parked and not service.alive():
                        logger.error(f"{name} exited with code {service.returncode}")
                        restart(name, crashed=True)
        except SupervisorError as exc:
            logger.error(str(exc))
            code = 1
        finally:
            watcher_stop.set()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.stop_all()
        return code

    def _drain_requests(self) -> list[str]:
        names: list[str] = []
        while True:
            try:
                name = self._requests.get_nowait()
            except Empty:
                return names
            if name not in names:
                names.append(name)

    def _start_watchers(self, stop: threading.Event):
        watched = [spec for spec in self.specs.values() if spec.watch]
        if not watched:
            return
        try:
            from watchfiles import PythonFilter, watch
        except ImportError:
            logger.warning("watchfiles is not installed: files are not watched")
            return

        def run(name: str, paths: tuple[Path, ...]):
            changes = watch(
                *paths, stop_event=stop, watch_filter=PythonFilter(), raise_interrupt=False
            )
            for _ in changes:
                self._requests.put(name)

        for spec in watched:
            threading.Thread(
                target=run, args=(spec.name, spec.watch), daemon=True, name=f"watch-{spec.name}"
            ).start()


# --- the specs of the stack ---------------------------------------------------------------------


def redis_ping(settings: Settings) -> bool:
    client = redis.Redis(**get_redis_kwargs(settings))
    try:
        return client.ping()
    except (redis.RedisError, OSError):
        return False
    finally:
        client.close()


def _prepare_redis_socket(settings: Settings):
    sock = settings.redis_conn
    if not sock.exists():
        return
    if redis_ping(settings):
        raise FileExistsError(f"A redis server is already running on {sock}")
    logger.warning(f"removing stale redis socket {sock}")
    sock.unlink()


class CeleryProbe:
    """Ready when a worker answers a control ping."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._app: Celery | None = None

    def __call__(self) -> bool:
        if self._app is None:
            self._app = create_celery(self.settings)
        return bool(self._app.control.ping(timeout=1.0, limit=1))


def probe_socket_factory(host: str, port: int) -> Callable[[], bool]:
    def probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False
    return probe


def stack_specs(
    settings: Settings,
    *,
    services: Sequence[str] = SERVICES,
    reload: bool = False,
    include: Sequence[str] = (),
    celery_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> list[ServiceSpec]:
    """Specs for redis, the celery worker and the backend, in start order.

    `reload`: restart celery when python files change (the backend reloads itself).
    `include`: extra task modules for the worker (the tests register theirs this way).
    `celery_args`: passed on to the celery worker.
    """
    unknown = set(services) - set(SERVICES)
    if unknown:
        raise ValueError(f"unknown services: {sorted(unknown)}")
    python = [sys.executable, "-m", "encosim_server"]
    needs_redis = ("redis",) if "redis" in services else ()
    specs: list[ServiceSpec] = []

    if "redis" in services:
        if shutil.which("redis-server") is None:
            raise FileNotFoundError("Could not find the 'redis-server' binary")
        sock = settings.redis_conn
        specs.append(
            ServiceSpec(
                "redis",
                argv=[
                    "redis-server",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--port",
                    "0",
                    "--unixsocket",
                    str(sock),
                    "--unixsocketperm",
                    "700",
                ],
                ready=lambda: redis_ping(settings),
                prepare=lambda: _prepare_redis_socket(settings),
                tee_to=service_log_file(settings.logs_dir, "redis") if settings.logs_dir else None,
                cwd=cwd,
                ready_timeout=10,
            )
        )

    if "celery" in services:
        specs.append(
            ServiceSpec(
                "celery",
                argv=[*python, "worker", *[f"--include={m}" for m in include], "--", *celery_args],
                ready=CeleryProbe(settings),
                depends_on=needs_redis,
                watch=(PACKAGE_DIR,) if reload else (),
                cwd=cwd,
                ready_timeout=60,
            )
        )

    if "backend" in services:
        host = "127.0.0.1" if settings.bind in ("", "0.0.0.0", "::") else settings.bind
        specs.append(
            ServiceSpec(
                "backend",
                argv=[*python, "serve", *(["--reload"] if reload else [])],
                ready=probe_socket_factory(host, settings.port),
                depends_on=needs_redis,
                cwd=cwd,
                ready_timeout=60,
            )
        )
    return specs
