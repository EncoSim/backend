"""Helpers around the supervised redis and celery worker of the test suite.

The processes themselves are started by `encosim_server.supervisor` (see the `stack` fixture).
"""

import time

import redis

from encosim_server.celery import create_celery
from encosim_server.core.settings import Settings
from encosim_server.database import get_redis_kwargs
from encosim_server.supervisor import Service

QUEUE = "celery"  # name of celery's default queue (= the redis list that holds waiting messages)
GATE_PREFIX = "test:gate:"


def gate_key(task_id: str) -> str:
    return f"{GATE_PREFIX}{task_id}"


def clean_redis(settings: Settings):
    """Delete what tests leave behind, but not celery's own bookkeeping.

    (FLUSHALL would also delete the queue bindings the running worker declared at startup.)
    """
    client = redis.Redis(**get_redis_kwargs(settings))
    try:
        for pattern in ("celery-task-meta-*", "limits:*", f"{GATE_PREFIX}*"):
            for key in client.scan_iter(pattern):
                client.delete(key)
        client.delete(QUEUE)
    finally:
        client.close()


def open_all_gates(settings: Settings):
    """Let every task that waits for a gate (tests/celery_tasks.py) finish."""
    client = redis.Redis(**get_redis_kwargs(settings))
    try:
        for key in client.scan_iter("limits:user:*:tasks"):
            for task_id in client.smembers(key):
                client.set(gate_key(str(task_id)), 1, ex=60)
    finally:
        client.close()


class WorkerControl:
    """Asks the running test worker whether it is busy."""

    def __init__(self, settings: Settings, service: Service):
        self.settings = settings
        self.service = service
        self.app = create_celery(settings)  # only used to send control commands

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

    def wait_idle(self, timeout: float):
        deadline = time.monotonic() + timeout
        while not self.is_idle():
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"worker still busy after {timeout}s (a test left a task behind)\n"
                    f"{self.service.output_tail()}"
                )
            time.sleep(0.2)


def running_pid(worker: WorkerControl, task_id: str) -> int | None:
    report = worker.app.control.inspect(timeout=2, limit=1).active() or {}
    for active_tasks in report.values():
        for task in active_tasks:
            if task["id"] == task_id:
                return task.get("worker_pid")
    return None
