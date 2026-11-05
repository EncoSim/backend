"""Endpoint tests for the simulation router (/simu).

Only `test_real_simulation_end_to_end` runs the actual simulator.
Others run fake tasks to keep tests deterministic and fast
"""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import redis
from _pytest.monkeypatch import MonkeyPatch
from celery.result import AsyncResult
from httpx import AsyncClient

from encosim_server.core.settings import Settings
from tests import celery_tasks as tasks
from tests import infra
from tests.helpers import eventually, is_running, simulation_payload
from tests.infra import gate_key, running_pid
from tests.routers.conftest import LoggedIn, LoginAs


@pytest.fixture
def use_task(monkeypatch: MonkeyPatch):
    """Make POST /simu run a test task instead of the real simulator."""

    def use(task: Any):
        monkeypatch.setattr("encosim_server.simulation.manager.handle_simulation", task)

    return use


@pytest.fixture
def router_wait(monkeypatch: MonkeyPatch):
    """Cap how long POST /simu waits for a result (the service defaults to 15 s).

    Tests can change `router_wait.seconds`; the default is generous enough for a cold worker.
    """
    cap = SimpleNamespace(seconds=5.0)
    original = AsyncResult.get

    def get(self: AsyncResult, *args: Any, timeout: float | None = None, **kwargs: Any):
        wait = cap.seconds if timeout is None else min(timeout, cap.seconds)
        return original(self, *args, timeout=wait, **kwargs)

    monkeypatch.setattr(AsyncResult, "get", get)
    return cap


async def job_ids(user: LoggedIn) -> list[str]:
    r = await user.client.get("/simu")
    assert r.status_code == 200
    return r.json()["ids"]


async def no_jobs(user: LoggedIn) -> bool:
    return await job_ids(user) == []


# --- access and validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/simu"), ("GET", "/simu"), ("GET", "/simu/anything")],
)
async def test_anonymous_requests_are_rejected(client: AsyncClient, method: str, path: str):
    r = await client.request(method, path, json=simulation_payload() if method == "POST" else None)

    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(repetitions=0),
        lambda p: p.update(teams=[]),
        lambda p: p.update(teams=p["teams"][:1]),  # a simulation needs two teams at least
        lambda p: p["teams"].__setitem__(0, []),  # ... and no team may be empty
    ],
    ids=["no-repetitions", "no-teams", "one-team", "empty-team"],
)
async def test_malformed_config_is_a_validation_error(
    login_as: LoginAs, mutate: Callable[[dict[str, Any]], None]
):
    alice = await login_as()
    payload = simulation_payload()
    mutate(payload)

    r = await alice.client.post("/simu", json=payload)

    assert r.status_code == 422


async def test_too_many_repetitions(login_as: LoginAs, settings: Settings):
    alice = await login_as()
    limit = settings.max_simulation_repetitions

    r = await alice.client.post("/simu", json=simulation_payload(repetitions=limit + 1))

    assert r.status_code == 403
    assert r.json()["code"] == "too_many_repetitions"
    assert r.json()["detail"] == {"allowed": limit}


async def test_too_many_participants(login_as: LoginAs, settings: Settings):
    alice = await login_as()
    limit = settings.max_simulation_participants
    sizes = (limit - 1, 2)  # one more member than allowed, each team on its own is fine

    r = await alice.client.post("/simu", json=simulation_payload(team_sizes=sizes))

    assert r.status_code == 403
    assert r.json()["code"] == "too_many_participants"
    assert r.json()["detail"] == {"allowed": limit}


# --- running jobs --------------------------------------------------------------------------------


@pytest.mark.usefixtures("worker")
async def test_finished_simulation_is_returned_directly(
    login_as: LoginAs, use_task: Callable[[Any], None]
):
    use_task(tasks.summary_task)
    alice = await login_as()

    r = await alice.client.post("/simu", json=simulation_payload(repetitions=3))

    assert r.status_code == 202
    assert r.json() == {"status": "success", "summary": "repetitions=3"}
    await eventually(lambda: no_jobs(alice), what="slot released")


@pytest.mark.usefixtures("worker")
async def test_crashed_simulation_is_reported_without_leaking_details(
    login_as: LoginAs, use_task: Callable[[Any], None]
):
    use_task(tasks.failing_task)
    alice = await login_as()

    r = await alice.client.post("/simu", json=simulation_payload())

    assert r.status_code == 202
    assert r.json() == {"status": "failed", "summary": "critical failure"}
    assert "boom" not in r.text
    await eventually(lambda: no_jobs(alice), what="slot released")


@pytest.mark.usefixtures("worker")
async def test_slow_simulation_returns_a_task_id_to_poll(
    login_as: LoginAs,
    use_task: Callable[[Any], None],
    router_wait: SimpleNamespace,
    redis_client: redis.Redis,
):
    use_task(tasks.gated_task)
    router_wait.seconds = 0.5
    alice = await login_as()

    r = await alice.client.post("/simu", json=simulation_payload())

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    task_id = body["task_id"]
    assert task_id.startswith(alice.user.user_id.hex + "+")
    assert await job_ids(alice) == [task_id]
    assert (await alice.client.get(f"/simu/{task_id}")).json() == body

    redis_client.set(gate_key(task_id), 1, ex=60)  # let the task finish

    async def status():
        return (await alice.client.get(f"/simu/{task_id}")).json()

    done = await eventually(lambda: _not_pending(status()), what="job finished")
    assert done == {"status": "success", "summary": "released"}
    assert await job_ids(alice) == []


async def _not_pending(coro: Awaitable[dict[str, Any]]) -> dict[str, Any] | None:
    body = await coro
    return body if body["status"] != "pending" else None


@pytest.mark.usefixtures("worker")
async def test_users_cannot_start_more_simulations_than_allowed(
    login_as: LoginAs,
    use_task: Callable[[Any], None],
    router_wait: SimpleNamespace,
    settings: Settings,
):
    use_task(tasks.gated_task)
    router_wait.seconds = 0.2
    alice = await login_as("google:alice")
    bob = await login_as("google:bob")
    limit = settings.max_simulations

    for _ in range(limit):
        assert (await alice.client.post("/simu", json=simulation_payload())).status_code == 202
    r = await alice.client.post("/simu", json=simulation_payload())

    assert r.status_code == 429
    assert r.json()["code"] == "too_many_simulations"
    assert r.json()["detail"] == {"allowed": limit}
    assert len(await job_ids(alice)) == limit
    # the limit is per user
    assert (await bob.client.post("/simu", json=simulation_payload())).status_code == 202


# --- job listing and status -----------------------------------------------------------------------


@pytest.mark.usefixtures("worker")
async def test_jobs_are_private_to_their_owner(
    login_as: LoginAs, use_task: Callable[[Any], None], router_wait: SimpleNamespace
):
    use_task(tasks.gated_task)
    router_wait.seconds = 0.2
    alice = await login_as("google:alice")
    bob = await login_as("google:bob")
    started = await alice.client.post("/simu", json=simulation_payload())
    task_id = started.json()["task_id"]

    assert await job_ids(alice) == [task_id]
    assert await job_ids(bob) == []

    r = await bob.client.get(f"/simu/{task_id}")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


async def test_unknown_job_ids_are_not_found(login_as: LoginAs):
    alice = await login_as()
    unknown_but_alices = f"{alice.user.user_id.hex}+{uuid4().hex}"

    for task_id in ("garbage", uuid4().hex, unknown_but_alices):
        r = await alice.client.get(f"/simu/{task_id}")
        assert r.status_code == 404, task_id
        assert r.json()["code"] == "not_found"


# --- time limits (the "task never ends" case) -----------------------------------------------------


@pytest.mark.usefixtures("worker")
@pytest.mark.parametrize(
    "task",
    [tasks.sleepy_task, tasks.stubborn_task],
    ids=["soft-limit", "hard-limit-signals-ignored"],
)
async def test_runaway_simulation_is_reported_as_timed_out(
    login_as: LoginAs, use_task: Callable[[Any], None], router_wait: SimpleNamespace, task: Any
):
    use_task(task)
    router_wait.seconds = 0.5
    alice = await login_as()

    started = await alice.client.post("/simu", json=simulation_payload())
    assert started.json()["status"] == "pending"
    task_id = started.json()["task_id"]

    async def status():
        return (await alice.client.get(f"/simu/{task_id}")).json()

    done = await eventually(lambda: _not_pending(status()), timeout=20, what="job timed out")
    assert done == {"status": "failed", "summary": "simulation timed out"}
    history = await alice.client.get("/simu/history")
    assert any(
        item["task_id"] == task_id and item["summary"] == "simulation timed out"
        for item in history.json()
    )

    # polling a finished job frees its slot (a killed process cannot do that itself) ...
    assert await job_ids(alice) == []
    # ... and the worker is still able to run the next simulation
    use_task(tasks.summary_task)
    router_wait.seconds = 5
    again = await alice.client.post("/simu", json=simulation_payload())
    assert again.json()["status"] == "success"


# --- task aborting --------------------------------------------------------------------------------


# @pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
@pytest.mark.usefixtures("worker")
async def test_a_running_job_that_ignores_signals_can_be_aborted(
    login_as: LoginAs,
    use_task: Callable[[Any], None],
    router_wait: SimpleNamespace,
    worker_process: infra.WorkerControl,
):
    use_task(tasks.unkillable_task)
    router_wait.seconds = 0.5
    alice = await login_as()
    task_id = (await alice.client.post("/simu", json=simulation_payload())).json()["task_id"]
    pid = await eventually(lambda: running_pid(worker_process, task_id), what="task running")

    r = await alice.client.delete(f"/simu/{task_id}")

    assert r.status_code == 200
    await eventually(lambda: not is_running(pid), timeout=5, what="pool process killed")
    status = await alice.client.get(f"/simu/{task_id}")
    assert status.json() == {"status": "failed", "summary": "simulation aborted"}
    assert await job_ids(alice) == []


@pytest.mark.usefixtures("worker")
async def test_a_queued_job_never_runs_once_it_is_aborted(
    login_as: LoginAs,
    use_task: Callable[[Any], None],
    router_wait: SimpleNamespace,
    redis_client: redis.Redis,
    worker_process: infra.WorkerControl,
):
    router_wait.seconds = 0.2
    alice = await login_as()
    use_task(tasks.gated_task)
    running = []
    for _ in range(2):  # the test worker runs two tasks at a time (WORKER_ARGS in conftest.py)
        r = await alice.client.post("/simu", json=simulation_payload())
        running.append(r.json()["task_id"])
    use_task(tasks.summary_task)  # would end as "success" if the worker ever ran it
    queued = (await alice.client.post("/simu", json=simulation_payload())).json()["task_id"]

    assert (await alice.client.delete(f"/simu/{queued}")).status_code == 200
    for task_id in running:
        redis_client.set(gate_key(task_id), 1, ex=60)
    await eventually(lambda: no_jobs(alice), what="running jobs finished")
    worker_process.wait_idle(timeout=15)  # the worker has gone through its queue

    status = await alice.client.get(f"/simu/{queued}")
    assert status.json() == {"status": "failed", "summary": "simulation aborted"}
    history = await alice.client.get("/simu/history")
    assert any(
        item["task_id"] == queued and item["summary"] == "simulation aborted"
        for item in history.json()
    )


@pytest.mark.usefixtures("worker")
async def test_only_the_owner_can_abort_a_job(
    login_as: LoginAs, use_task: Callable[[Any], None], router_wait: SimpleNamespace
):
    use_task(tasks.gated_task)
    router_wait.seconds = 0.2
    alice, bob = await login_as("google:alice"), await login_as("google:bob")
    task_id = (await alice.client.post("/simu", json=simulation_payload())).json()["task_id"]
    unknown = f"{alice.user.user_id.hex}+{uuid4().hex}"

    for user, target in ((bob, task_id), (alice, unknown), (alice, "garbage")):
        r = await user.client.delete(f"/simu/{target}")
        assert r.status_code == 404, target
        assert r.json()["code"] == "not_found"
    assert await job_ids(alice) == [task_id]  # still there, still running


@pytest.mark.usefixtures("worker")
async def test_a_finished_job_cannot_be_aborted(
    login_as: LoginAs,
    use_task: Callable[[Any], None],
    router_wait: SimpleNamespace,
    redis_client: redis.Redis,
):
    use_task(tasks.gated_task)
    router_wait.seconds = 0.2
    alice = await login_as()
    task_id = (await alice.client.post("/simu", json=simulation_payload())).json()["task_id"]
    redis_client.set(gate_key(task_id), 1, ex=60)
    await eventually(lambda: no_jobs(alice), what="job finished")

    r = await alice.client.delete(f"/simu/{task_id}")

    assert r.status_code == 404  # its slot is gone, so there is nothing to abort


@pytest.mark.usefixtures("worker")
async def test_finished_history_keeps_configuration_and_result(
    login_as: LoginAs, use_task: Callable[[Any], None]
):
    use_task(tasks.summary_task)
    alice = await login_as()
    config = simulation_payload(repetitions=3)

    submitted = await alice.client.post("/simu", json=config)
    assert submitted.status_code == 202
    assert submitted.json() == {"status": "success", "summary": "repetitions=3"}

    history = await alice.client.get("/simu/history")
    assert history.status_code == 200
    assert len(history.json()) == 1
    record = history.json()[0]
    assert record["status"] == "success"
    assert record["config"] == config
    assert record["result"] == "repetitions=3"
    assert record["summary"] == "repetitions=3"
    task_id = record["task_id"]

    fetched = await alice.client.get(f"/simu/history/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json() == record

    deleted = await alice.client.delete(f"/simu/history/{task_id}")
    assert deleted.status_code == 200
    assert (await alice.client.get("/simu/history")).json() == []


@pytest.mark.usefixtures("worker")
async def test_failed_simulations_are_kept_in_history(
    login_as: LoginAs, use_task: Callable[[Any], None]
):
    use_task(tasks.failing_task)
    alice = await login_as()

    submitted = await alice.client.post("/simu", json=simulation_payload())
    assert submitted.status_code == 202
    assert submitted.json() == {"status": "failed", "summary": "critical failure"}

    history = await alice.client.get("/simu/history")
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["status"] == "failed"
    assert history.json()[0]["config"] == simulation_payload()
    assert history.json()[0]["result"] is None


@pytest.mark.usefixtures("worker")
async def test_history_retains_only_the_latest_ten_per_user(
    login_as: LoginAs, use_task: Callable[[Any], None]
):
    use_task(tasks.summary_task)
    alice = await login_as()
    for _ in range(11):
        response = await alice.client.post("/simu", json=simulation_payload())
        assert response.status_code == 202
        assert response.json()["status"] == "success"

    history = await alice.client.get("/simu/history")
    assert history.status_code == 200
    assert len(history.json()) == 10


@pytest.mark.usefixtures("worker")
async def test_finished_history_is_private(login_as: LoginAs, use_task: Callable[[Any], None]):
    use_task(tasks.summary_task)
    alice = await login_as("google:alice")
    bob = await login_as("google:bob")
    submitted = await alice.client.post("/simu", json=simulation_payload())
    assert submitted.status_code == 202
    record = (await alice.client.get("/simu/history")).json()[0]
    task_id = record["task_id"]

    assert (await bob.client.get("/simu/history")).json() == []
    assert (await bob.client.get(f"/simu/history/{task_id}")).status_code == 404
    assert (await bob.client.delete(f"/simu/history/{task_id}")).status_code == 404


# --- the real simulator ---------------------------------------------------------------------------


@pytest.mark.usefixtures("worker")
async def test_real_simulation_end_to_end(login_as: LoginAs, router_wait: SimpleNamespace):
    """Runs `handle_simulation` for real. Its duration depends on the simulator."""
    router_wait.seconds = 60
    alice = await login_as()

    r = await alice.client.post("/simu", json=simulation_payload(repetitions=1))

    assert r.status_code == 202
    body = r.json()
    if body["status"] == "pending":
        task_id = body["task_id"]

        async def status():
            return (await alice.client.get(f"/simu/{task_id}")).json()

        body = await eventually(lambda: _not_pending(status()), timeout=120, what="simulation")
    assert body["status"] == "success"
