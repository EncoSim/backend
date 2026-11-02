"""Tasks for the test suite, loaded by the test worker (`--include tests.celery_tasks`) and by the
test process.

They take the same `config` argument as `handle_simulation`, so the simulation router can run
them instead of the real simulator (see `use_task` in tests/test_simulation.py).

Every task sets its time limits explicitly (like `handle_simulation` does), so none of them
depends on the `celery_*_timeout` settings.
"""

import signal
import time
from typing import Any

import redis
from celery import shared_task

from encosim_server.celery import LimitedTask
from encosim_server.database import get_redis_kwargs
from tests.infra import gate_key

LONG = dict(soft_time_limit=30, time_limit=60)
SHORT = dict(soft_time_limit=2, time_limit=4)  # for tasks that are meant to hit the limits


@shared_task(base=LimitedTask, bind=True, **LONG)
def summary_task(self: LimitedTask, config: dict[str, Any]) -> str:
    return f"repetitions={config['repetitions']}"


@shared_task(base=LimitedTask, bind=True, **LONG)
def failing_task(self: LimitedTask, config: dict[str, Any]) -> str:
    raise RuntimeError("boom")


@shared_task(base=LimitedTask, bind=True, **LONG)
def gated_task(self: LimitedTask, config: dict[str, Any]) -> str:
    """Runs until the test sets the key `gate_key(task_id)`, so tests control when it finishes."""
    client = redis.Redis(**get_redis_kwargs(self.app.user_limiter.settings))
    key = gate_key(self.request.id)  # type: ignore - celery has terrible typing support
    deadline = time.monotonic() + 25
    while not client.exists(key):
        if time.monotonic() > deadline:
            raise TimeoutError("gate was never opened")
        time.sleep(0.05)
    return "released"


@shared_task(base=LimitedTask, bind=True, **SHORT)
def sleepy_task(self: LimitedTask, config: dict[str, Any]) -> str:
    """Never returns, but listens: the soft time limit raises SoftTimeLimitExceeded in here."""
    while True:
        time.sleep(0.1)


@shared_task(base=LimitedTask, bind=True, **SHORT)
def stubborn_task(self: LimitedTask, config: dict[str, Any]) -> str:
    """Busy loop that ignores the soft time limit (SIGUSR1) and SIGTERM.

    Only the hard time limit (the pool kills the process with SIGKILL) can end this one.
    """
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)


@shared_task(base=LimitedTask, bind=True, **LONG)
def unkillable_task(self: LimitedTask, config: dict[str, Any]) -> str:
    """Busy loop that ignores SIGTERM and the soft limit and reports its pid.

    The limits are long, so only an explicit SIGKILL (revoke) can end it in time.
    """
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
