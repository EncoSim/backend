"""Starts the processes the tests need: a private redis-server and a celery worker.

Both run in their own process group (POSIX only, like the unix sockets the app uses), so teardown
can SIGKILL the whole group. This matters for the worker: a task that ignores every signal would
otherwise survive as an orphaned pool process.

DRAFT: not run yet.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import redis
from celery import Celery

from encosim_server.celery import create_celery
from encosim_server.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
QUEUE = "celery"  # name of celery's default queue (= the redis list that holds waiting messages)
GATE_PREFIX = "test:gate:"


def gate_key(task_id: str) -> str:
    return f"{GATE_PREFIX}{task_id}"


def iso_duration(value: timedelta) -> str:
    return f"PT{value.total_seconds()}S"


def tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "<no log>"


def kill_group(proc: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    """Stop `proc` and everything it spawned (celery's pool children, for example)."""

    def send(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)  # `proc` leads its own group, see start_new_session below
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    send(signal.SIGKILL)  # also catches pool children that ignore SIGTERM
    proc.wait()


# --- redis ---------------------------------------------------------------------------------------


def _ping(sock: Path) -> bool:
    client = redis.Redis(unix_socket_path=str(sock))
    try:
        return client.ping()
    except (redis.RedisError, OSError):
        return False
    finally:
        client.close()


@dataclass
class RedisServer:
    proc: subprocess.Popen[bytes]
    dir: Path
    sock: Path
    log: Path

    def stop(self) -> None:
        kill_group(self.proc, grace=3)
        shutil.rmtree(self.dir, ignore_errors=True)


def start_redis(timeout: float = 10.0) -> RedisServer:
    exe = shutil.which("redis-server")
    if exe is None:
        raise RuntimeError("redis-server not found on PATH (the tests start their own)")

    # unix socket paths are limited to ~100 characters, so pytest's tmp dirs may be too long
    run_dir = Path(tempfile.mkdtemp(prefix="encosim-", dir="/tmp"))
    sock, log = run_dir / "redis.sock", run_dir / "redis.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [
                exe,
                "--port",
                "0",  # unix socket only, like the real setup
                "--unixsocket",
                str(sock),
                "--unixsocketperm",
                "700",
                "--dir",
                str(run_dir),
                "--save",
                "",
                "--appendonly",
                "no",
            ],  # fmt: skip
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    server = RedisServer(proc, run_dir, sock, log)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        if _ping(sock):
            return server
        time.sleep(0.05)
    message = f"redis-server did not start:\n{tail(log)}"
    server.stop()
    raise RuntimeError(message)


def clean_redis(sock: Path) -> None:
    """Delete what tests leave behind, but not celery's own bookkeeping.

    (FLUSHALL would also delete the queue bindings the running worker declared at startup.)
    """
    client = redis.Redis(unix_socket_path=str(sock))
    try:
        for pattern in ("celery-task-meta-*", "limits:*", f"{GATE_PREFIX}*"):
            for key in client.scan_iter(pattern):
                client.delete(key)
        client.delete(QUEUE)
    finally:
        client.close()


def open_all_gates(sock: Path) -> None:
    """Let every task that waits for a gate (tests/celery_tasks.py) finish."""
    client = redis.Redis(unix_socket_path=str(sock), decode_responses=True)
    try:
        for key in client.scan_iter("limits:user:*:tasks"):
            for task_id in client.smembers(key):
                client.set(gate_key(str(task_id)), 1, ex=60)
    finally:
        client.close()


# --- celery worker --------------------------------------------------------------------------------


@dataclass
class Worker:
    proc: subprocess.Popen[bytes]
    log: Path
    app: Celery  # only used to send control commands (ping, inspect)
    settings: Settings

    def stop(self) -> None:
        kill_group(self.proc, grace=10)

    def is_idle(self) -> bool:
        inspect = self.app.control.inspect(timeout=2, limit=1)
        for report in (inspect.active(), inspect.reserved()):
            if report is None or any(report.values()):  # None: nobody answered
                return False
        client = redis.Redis(unix_socket_path=str(self.settings.redis_conn))
        try:
            return client.llen(QUEUE) == 0
        finally:
            client.close()

    def wait_idle(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self.is_idle():
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"worker still busy after {timeout}s (a test left a task behind)\n"
                    f"{tail(self.log)}"
                )
            time.sleep(0.2)


def start_worker(settings: Settings, env_file: Path, log: Path, timeout: float = 60.0) -> Worker:
    # The worker builds its own Settings from `env_file`. Environment variables win over the file,
    # so these three values are all it needs to share the private redis and the short timeouts.
    env = {
        **os.environ,
        "TEST_ENV_FILE": str(env_file),
        "ENCOSIM_REDIS_CONN": str(settings.redis_conn),
        "ENCOSIM_CELERY_SOFT_TIMEOUT": iso_duration(settings.celery_soft_timeout),
        "ENCOSIM_CELERY_HARD_TIMEOUT": iso_duration(settings.celery_hard_timeout),
    }
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.worker"],
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    worker = Worker(proc, log, create_celery(settings), settings)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        if worker.app.control.ping(timeout=1.0, limit=1):
            return worker
    message = f"celery worker did not start (exit code {proc.poll()}):\n{tail(log)}"
    worker.stop()
    raise RuntimeError(message)
