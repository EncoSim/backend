import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import redis

from encosim_server.core.settings import Settings
from encosim_server.logs import tagged_print

COLORS = {"main": "31", "redis": "33", "celery": "32", "backend": "36"}


class TaggedStdout:
    def __init__(self, wrapped, tag: str, color: str = "0"):
        self._wrapped = wrapped
        self._prefix = f"\x1b[{color}m{tag:<8}\x1b[0m | "
        self._main = threading.main_thread()
        self._lock = threading.Lock()
        self._at_line_start = True

    def write(self, data: str) -> int:
        if not data:
            return 0
        with self._lock:
            # Pump threads write their own tagged lines → let them through.
            if threading.current_thread() is not self._main:
                return self._wrapped.write(data)

            # Main thread: prefix the start of every line.
            out = []
            for ch in data:
                if self._at_line_start:
                    out.append(self._prefix)
                    self._at_line_start = False
                out.append(ch)
                if ch == "\n":
                    self._at_line_start = True
            self._wrapped.write("".join(out))
            return len(data)

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name):
        # Delegate .encoding, .isatty, .buffer, .fileno, etc.
        return getattr(self._wrapped, name)


class Service:
    def __init__(
        self,
        name: str,
        cmd: list[str],
        logs_dir: Path | None = None,
    ):
        self.name = name
        self._log_file = (
            (logs_dir / f"{name}.log").open("a", encoding="utf-8", buffering=1)
            if logs_dir
            else None
        )
        self.proc = subprocess.Popen(
            cmd,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group, so Ctrl+C only hits the supervisor
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            tagged_print(self.name, line)
            if self._log_file:
                self._log_file.write(line)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except ProcessLookupError:
            pass

    def stop(self, grace: float = 10.0) -> None:
        self._signal(signal.SIGTERM)
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        self._signal(signal.SIGKILL)  # also catches stragglers (reloader, pool children)
        self.proc.wait()
        self._thread.join(timeout=3)
        if self._log_file:
            self._log_file.close()


def wait_for_redis(settings: Settings, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    client = redis.Redis(unix_socket_path=str(settings.redis_conn))
    try:
        while time.monotonic() < deadline:
            try:
                if client.ping():
                    return
            except (redis.RedisError, OSError):
                time.sleep(0.1)
    finally:
        client.close()
    raise RuntimeError(f"redis did not come up on socket {settings.redis_conn}")


@contextmanager
def supervisor(settings: Settings) -> Generator[list[Service]]:
    """start redis, celery"""
    if shutil.which("redis-server") is None:
        raise FileNotFoundError("Could not find 'redis-server' binary")
    if settings.redis_conn.exists():
        raise FileExistsError(f"Redis server is already running on socket {settings.redis_conn}")

    services: list[Service] = []
    try:
        services.append(
            Service(
                "redis",
                [
                    "redis-server",
                    "--save",
                    "",
                    "--port",
                    "0",
                    "--unixsocket",
                    str(settings.redis_conn),
                ],
                logs_dir=settings.logs_dir,
            )
        )
        wait_for_redis(settings)
        # TODO: enable reloading for celery?
        services.append(Service("celery", [sys.executable, "-m", "encosim_server.celery"]))
        yield services
    finally:
        for service in reversed(services):
            service.stop()
