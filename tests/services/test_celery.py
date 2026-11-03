import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.result import AsyncResult

import encosim_server.simulation.tasks  # noqa: F401  (registers handle_simulation)
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources
from encosim_server.simulation.limiter import SimulationLimiter
from encosim_server.simulation.schemas import SimulationConfig
from tests import celery_tasks as tasks
from tests.helpers import eventually, simulation_payload
from tests.infra import gate_key

CONFIG = {"repetitions": 2}


@pytest.fixture
def limiter(resources: Resources) -> SimulationLimiter:
    return resources.celery.simulation_limiter


# --- task ids ------------------------------------------------------------------------------------


def test_task_id_encodes_its_owner():
    alice, bob = uuid4(), uuid4()
    task_id = SimulationLimiter.new_task_id(alice)

    assert SimulationLimiter.user_id_from_task_id(task_id) == alice
    assert SimulationLimiter.task_belongs_to(task_id, alice)
    assert not SimulationLimiter.task_belongs_to(task_id, bob)
    assert SimulationLimiter.new_task_id(alice) != task_id


def test_task_id_without_owner_is_not_recognized():
    assert SimulationLimiter.user_id_from_task_id("no-separator") is None


# --- limiter (redis only) ---------------------------------------------------------------------


async def test_acquire_registers_the_task_for_its_user(limiter: SimulationLimiter):
    user = uuid4()

    task_id = await limiter.acquire(user)

    assert isinstance(task_id, str)
    assert SimulationLimiter.task_belongs_to(task_id, user)
    assert limiter.task_exists(task_id)
    assert await limiter.active(user) == {task_id}


def test_unknown_task_does_not_exist(limiter: SimulationLimiter):
    assert not limiter.task_exists(SimulationLimiter.new_task_id(uuid4()))
    assert not limiter.task_exists("no-separator")


async def test_acquire_refuses_beyond_the_limit(limiter: SimulationLimiter):
    user = uuid4()
    for _ in range(limiter.limit):
        assert await limiter.acquire(user) is not None

    assert await limiter.acquire(user) is None


async def test_limit_is_per_user(limiter: SimulationLimiter):
    alice, bob = uuid4(), uuid4()
    for _ in range(limiter.limit):
        await limiter.acquire(alice)

    assert await limiter.acquire(bob) is not None


async def test_release_gives_the_slot_back(limiter: SimulationLimiter):
    user = uuid4()
    task_ids = [await limiter.acquire(user) for _ in range(limiter.limit)]
    assert await limiter.acquire(user) is None

    first = task_ids[0]
    assert first is not None
    limiter.release_sync(first)
    limiter.release_sync(first)  # releasing twice is harmless
    limiter.release_sync("no-separator")  # so are ids we do not know

    assert not limiter.task_exists(first)
    assert await limiter.acquire(user) is not None


async def test_concurrent_acquires_cannot_exceed_the_limit(limiter: SimulationLimiter):
    user = uuid4()

    results = await asyncio.gather(
        *(limiter.acquire(user) for _ in range(limiter.limit * 4))
    )

    assert sum(task_id is not None for task_id in results) == limiter.limit
    assert len(await limiter.active(user)) == limiter.limit


async def test_service_rolls_back_reservation_when_publish_fails(
    resources: Resources, monkeypatch: pytest.MonkeyPatch
):
    class BrokenTask:
        def apply_async(self, **_kwargs):
            raise ConnectionError("broker is down")

    owner_id = uuid4()
    monkeypatch.setattr("encosim_server.simulation.service.handle_simulation", BrokenTask())

    with pytest.raises(ConnectionError):
        await resources.simulations.submit(
            SimulationConfig.model_validate(simulation_payload()), owner_id
        )

    assert await resources.celery.simulation_limiter.active(owner_id) == set()
    assert await resources.simulations.records.find_one({"owner_id": owner_id}) is None


def test_app_is_configured_for_reliable_delivery(resources: Resources, settings: Settings):
    conf = resources.celery.conf
    assert isinstance(conf.broker_url, str)
    assert conf.broker_url.startswith("redis+socket://")
    assert conf.result_backend == "mongodb://"
    assert conf.mongodb_backend_settings["database"] == settings.mongo_db
    assert conf.result_expires is None  # history retention explicitly forgets old results
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_track_started is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.accept_content == ["json"]


def test_time_limits_from_settings_reach_celery(resources: Resources, settings: Settings):
    conf = resources.celery.conf
    assert conf.task_soft_time_limit == settings.celery_soft_timeout.total_seconds()
    assert conf.task_time_limit == settings.celery_hard_timeout.total_seconds()  # the hard limit


@pytest.mark.xfail(reason="timeout is set by app, not by the task itself")
def test_simulation_task_has_both_time_limits(resources: Resources, settings: Settings):
    task = resources.celery.tasks["encosim_server.simulation.tasks.handle_simulation"]

    assert task.soft_time_limit == settings.celery_soft_timeout.total_seconds()
    assert task.time_limit == settings.celery_hard_timeout.total_seconds()


# --- worker --------------------------------------------------------------------------------------


async def submit(
    resources: Resources, task: Any, user: UUID | None = None, **kwargs: Any
) -> AsyncResult:
    limiter = resources.celery.simulation_limiter
    task_id = await limiter.acquire(user or uuid4())
    assert task_id is not None
    try:
        return task.apply_async(kwargs={"config": kwargs.get("config", CONFIG)}, task_id=task_id)
    except Exception:
        limiter.release_sync(task_id)
        raise


async def finished(result: AsyncResult, timeout: float = 20) -> AsyncResult:
    await eventually(result.ready, timeout=timeout, what=f"task {result.id} finished")
    return result


async def slot_freed(resources: Resources, task_id: str):
    limiter = resources.celery.simulation_limiter
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
    limiter = resources.celery.simulation_limiter

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
async def test_hard_time_limit_stops_a_task_that_ignores_all_signals(resources: Resources):
    user = uuid4()
    hard = tasks.SHORT["time_limit"]

    stuck = await finished(await submit(resources, tasks.stubborn_task, user), timeout=hard + 10)

    assert stuck.state == "FAILURE"
    assert isinstance(stuck.result, TimeLimitExceeded)

    # the killed pool process is replaced: the worker keeps working
    after = await finished(await submit(resources, tasks.summary_task, user))
    assert after.state == "SUCCESS"
