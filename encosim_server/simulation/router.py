import logging
import traceback

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.exceptions import TimeoutError as CeleryTimeout
from celery.result import AsyncResult
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder

from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep
from encosim_server.simulation.schemas import (
    SimulationFinished,
    SimulationList,
    SimulationPending,
    SimulationResult,
    SimulatonConfig,
)
from encosim_server.simulation.tasks import handle_simulation

router = APIRouter(prefix="/simu", tags=["simu"])


@router.post(
    "",
    responses=errors.to_responses(
        errors.Unauthorized,
        errors.TooManyParticipants,
        errors.TooManyRepetitions,
    ),
)
async def simulate(
    res: ResourcesDep, user: ActiveUserDep, config: SimulatonConfig
) -> SimulationResult:
    # TODO: add priviledges
    if config.repetitions > res.settings.max_simulation_repetitions:
        raise errors.TooManyRepetitions(
            detail=errors.AllowedNumberDetail(allowed=res.settings.max_simulation_repetitions)
        )
    total_members = sum(len(t) for t in config.teams)
    if total_members > res.settings.max_simulation_participants:
        raise errors.TooManyRepetitions(
            detail=errors.AllowedNumberDetail(allowed=res.settings.max_simulation_participants)
        )

    # delegate simulation to celery
    result = await res.celery.user_limiter.submit(
        handle_simulation,
        user.user_id,
        config=jsonable_encoder(config),
    )
    if result is None:
        raise errors.TooManySimulations(
            detail=errors.AllowedNumberDetail(allowed=res.settings.max_simulations)
        )

    # wait a little for result
    try:
        await run_in_threadpool(result.get, timeout=15, propagate=False)
    except CeleryTimeout:
        pass
    assert result.id is not None
    return summarize(result, result.id)

@router.get("/jobs")
async def job_list(res: ResourcesDep, user: ActiveUserDep) -> SimulationList:
    return SimulationList(ids=list(await res.celery.user_limiter.active(user.user_id)))

@router.get("/jobs/{task_id}")
async def job_status(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> SimulationResult:
    if not res.celery.user_limiter.task_belongs_to(task_id, user.user_id):
        raise errors.NotFound()

    result = AsyncResult(task_id, app=res.celery)
    if result.state == "PENDING" and not res.celery.user_limiter.task_exists(task_id):
        raise errors.NotFound()
    summary = summarize(result, task_id)
    if summary.status != "pending":
        res.celery.user_limiter.release_sync(task_id)
    return summary


logger = logging.getLogger(__name__)


def summarize(result: AsyncResult, task_id: str) -> SimulationResult:
    if result.failed():
        has_timed_out = isinstance(result.info, (TimeLimitExceeded, SoftTimeLimitExceeded))
        if not has_timed_out and isinstance(result.info, Exception):
            logger.error(
                "task %s failed: %r\n%s\nargs: %s\nkwargs: %s",
                result.id,
                result.info,
                result.traceback,
                result.args,
                result.kwargs,
            )
        return SimulationFinished(
            status="failed",
            summary="simulation timed out" if has_timed_out else "critical failure",
        )

    if result.successful():
        return SimulationFinished(status="success", summary=result.result)

    return SimulationPending(task_id=task_id)
