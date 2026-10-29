"""Tests for the celery integration: `UserTaskLimiter`, `LimitedTask` and a real worker.

The limiter tests only need the private redis. The worker tests use the `worker` fixture, which
runs a real worker process (see conftest.py and tests/worker.py), so they cover serialization,
the result backend and the time limits for real.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.result import AsyncResult

from encosim_server.celery import UserTaskLimiter
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources
from tests import celery_tasks as tasks
from tests.helpers import eventually
from tests.infra import gate_key

CONFIG = {"repetitions": 2}


class FakeTask:
    """Stands in for a celery task: `submit` only calls `apply_async`."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def apply_async(self, kwargs: dict[str, Any], task_id: str):
        if self.fail:
            raise ConnectionError("broker is down")
        self.sent.append((task_id, kwargs))
        return SimpleNamespace(id=task_id)


@pytest.fixture
def limiter(settings: Settings) -> UserTaskLimiter:
    return UserTaskLimiter(settings)


# --- task ids ------------------------------------------------------------------------------------


def test_task_id_encodes_its_owner():
    alice, bob = uuid4(), uuid4()
    task_id = UserTaskLimiter.new_task_id(alice)

    assert UserTaskLimiter.user_id_from_task_id(task_id) == alice
    assert UserTaskLimiter.task_belongs_to(task_id, alice)
    assert not UserTaskLimiter.task_belongs_to(task_id, bob)
    assert UserTaskLimiter.new_task_id(alice) != task_id


def test_task_id_without_owner_is_not_recognized():
    assert UserTaskLimiter.user_id_from_task_id("no-separator") is None


# --- limiter (redis only) ---------------------------------------------------------------------


async def test_submit_registers_the_task_for_its_user(limiter: UserTaskLimiter):
    user, task = uuid4(), FakeTask()

    result = await limiter.submit(task, user, config=CONFIG)

    assert result is not None
    assert task.sent == [(result.id, {"config": CONFIG})]
    assert isinstance(result.id, str)
    assert UserTaskLimiter.task_belongs_to(result.id, user)
    assert limiter.task_exists(result.id)
    assert await limiter.active(user) == {result.id}


def test_unknown_task_does_not_exist(limiter: UserTaskLimiter):
    assert not limiter.task_exists(UserTaskLimiter.new_task_id(uuid4()))
    assert not limiter.task_exists("no-separator")


async def test_submit_refuses_beyond_the_limit(limiter: UserTaskLimiter):
    user, task = uuid4(), FakeTask()
    for _ in range(limiter.limit):
        assert await limiter.submit(task, user, config=CONFIG) is not None

    assert await limiter.submit(task, user, config=CONFIG) is None
    assert len(task.sent) == limiter.limit  # the refused one never reached the broker


async def test_limit_is_per_user(limiter: UserTaskLimiter):
    alice, bob, task = uuid4(), uuid4(), FakeTask()
    for _ in range(limiter.limit):
        await limiter.submit(task, alice, config=CONFIG)

    assert await limiter.submit(task, bob, config=CONFIG) is not None


async def test_release_gives_the_slot_back(limiter: UserTaskLimiter):
    user, task = uuid4(), FakeTask()
    results = [await limiter.submit(task, user, config=CONFIG) for _ in range(limiter.limit)]
    assert await limiter.submit(task, user, config=CONFIG) is None

    first = results[0]
    assert first is not None
    assert isinstance(first.id, str)
    limiter.release_sync(first.id)
    limiter.release_sync(first.id)  # releasing twice is harmless
    limiter.release_sync("no-separator")  # so are ids we do not know

    assert not limiter.task_exists(first.id)
    assert await limiter.submit(task, user, config=CONFIG) is not None


async def test_failed_publish_gives_the_slot_back(limiter: UserTaskLimiter):
    user = uuid4()

    with pytest.raises(ConnectionError):
        await limiter.submit(FakeTask(fail=True), user, config=CONFIG)

    assert await limiter.active(user) == set()


async def test_concurrent_submits_cannot_exceed_the_limit(limiter: UserTaskLimiter):
    user, task = uuid4(), FakeTask()

    results = await asyncio.gather(
        *(limiter.submit(task, user, config=CONFIG) for _ in range(limiter.limit * 4))
    )

    assert sum(r is not None for r in results) == limiter.limit
    assert len(await limiter.active(user)) == limiter.limit


def test_app_is_configured_for_reliable_delivery(resources: Resources):
    conf = resources.celery.conf
    assert isinstance(conf.broker_url, str)
    assert conf.broker_url.startswith("redis+socket://")
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.accept_content == ["json"]


# --- worker --------------------------------------------------------------------------------------


async def submit(
    resources: Resources, task: Any, user: UUID | None = None, **kwargs: Any
) -> AsyncResult:
    result = await resources.celery.user_limiter.submit(
        task, user or uuid4(), config=kwargs.get("config", CONFIG)
    )
    assert result is not None
    return result


async def finished(result: AsyncResult, timeout: float = 20) -> AsyncResult:
    await eventually(result.ready, timeout=timeout, what=f"task {result.id} finished")
    return result


async def slot_freed(resources: Resources, task_id: str):
    limiter = resources.celery.user_limiter
    await eventually(lambda: not limiter.task_exists(task_id), what="slot released")


@pytest.mark.usefixtures("worker")
async def test_worker_runs_a_task_and_frees_the_slot(resources: Resources):
    result = await finished(await submit(resources, tasks.summary_task))

    assert result.state == "SUCCESS"
    assert result.result == "repetitions=2"
    assert isinstance(result.id, str)
    await slot_freed(resources, result.id)


@pytest.mark.usefixtures("worker")
async def test_failed_task_is_reported_and_frees_the_slot(resources: Resources):
    result = await finished(await submit(resources, tasks.failing_task))

    assert result.state == "FAILURE"
    assert isinstance(result.result, RuntimeError)
    assert isinstance(result.id, str)
    await slot_freed(resources, result.id)


@pytest.mark.usefixtures("worker")
async def test_slot_is_held_while_the_task_runs(resources: Resources, redis_client: redis.Redis):
    user = uuid4()
    result = await submit(resources, tasks.gated_task, user)
    limiter = resources.celery.user_limiter

    assert await limiter.active(user) == {result.id}
    assert not result.ready()

    assert isinstance(result.id, str)
    redis_client.set(gate_key(result.id), 1, ex=60)
    await finished(result)

    assert result.result == "released"
    await slot_freed(resources, result.id)


@pytest.mark.usefixtures("worker")
async def test_soft_time_limit_stops_a_task_that_listens(resources: Resources):
    result = await finished(await submit(resources, tasks.sleepy_task))

    assert result.state == "FAILURE"
    assert isinstance(result.result, SoftTimeLimitExceeded)
    assert isinstance(result.id, str)
    await slot_freed(resources, result.id)


@pytest.mark.usefixtures("worker")
async def test_hard_time_limit_stops_a_task_that_ignores_all_signals(
    resources: Resources, settings: Settings
):
    user = uuid4()
    hard = settings.celery_hard_timeout.total_seconds()

    stuck = await finished(await submit(resources, tasks.stubborn_task, user), timeout=hard + 10)

    assert stuck.state == "FAILURE"
    assert isinstance(stuck.result, TimeLimitExceeded)

    # the killed pool process is replaced: the worker keeps working
    after = await finished(await submit(resources, tasks.summary_task, user))
    assert after.state == "SUCCESS"
