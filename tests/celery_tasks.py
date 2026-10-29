"""Tasks for the test suite, loaded by the worker (tests/worker.py) and by the test process.

They take the same `config` argument as `handle_simulation`, so the simulation router can run
them instead of the real simulator (see `use_task` in tests/test_simulation.py).

Every task sets its time limits explicitly (like `handle_simulation` does), so none of them
depends on the `celery_*_timeout` settings.
"""

import signal
import time

import redis
from celery import shared_task

from encosim_server.celery import LimitedTask
from tests.infra import gate_key

QUICK = dict(soft_time_limit=30, time_limit=60)
SHORT = dict(soft_time_limit=2, time_limit=4)  # for tasks that are meant to hit the limits


@shared_task(base=LimitedTask, bind=True, **QUICK)
def summary_task(self, config: dict) -> str:
    return f"repetitions={config['repetitions']}"


@shared_task(base=LimitedTask, bind=True, **QUICK)
def failing_task(self, config: dict) -> str:
    raise RuntimeError("boom")


@shared_task(base=LimitedTask, bind=True, **QUICK)
def gated_task(self, config: dict) -> str:
    """Runs until the test sets the key `gate_key(task_id)`, so tests control when it finishes."""
    client = redis.Redis(unix_socket_path=str(self.app.user_limiter.settings.redis_conn))
    key = gate_key(self.request.id)
    deadline = time.monotonic() + 25
    while not client.exists(key):
        if time.monotonic() > deadline:
            raise TimeoutError("gate was never opened")
        time.sleep(0.05)
    return "released"


@shared_task(base=LimitedTask, bind=True, **SHORT)
def sleepy_task(self, config: dict) -> str:
    """Never returns, but listens: the soft time limit raises SoftTimeLimitExceeded in here."""
    while True:
        time.sleep(0.1)


@shared_task(base=LimitedTask, bind=True, **SHORT)
def stubborn_task(self, config: dict) -> str:
    """Busy loop that ignores the soft time limit (SIGUSR1) and SIGTERM.

    Only the hard time limit (the pool kills the process with SIGKILL) can end this one.
    """
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
