from fastapi import APIRouter

from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep
from encosim_server.simulation.schemas import (
    SimulationConfig,
    SimulationList,
    SimulationRecord,
    SimulationResult,
)

router = APIRouter(prefix="/simu", tags=["simu"])


@router.post(
    "",
    responses=errors.to_responses(
        errors.Unauthorized,
        errors.TooManyParticipants,
        errors.TooManyRepetitions,
        errors.TooManySimulations,
    ),
)
async def simulate(
    res: ResourcesDep, user: ActiveUserDep, config: SimulationConfig
) -> SimulationResult:
    if config.repetitions > res.settings.max_simulation_repetitions:
        raise errors.TooManyRepetitions(
            detail=errors.AllowedNumberDetail(allowed=res.settings.max_simulation_repetitions)
        )
    total_members = sum(len(team) for team in config.teams)
    if total_members > res.settings.max_simulation_participants:
        raise errors.TooManyParticipants(
            detail=errors.AllowedNumberDetail(allowed=res.settings.max_simulation_participants)
        )
    return await res.simulations.submit(config, user.user_id)


@router.get("", responses=errors.to_responses(errors.Unauthorized))
async def job_list(res: ResourcesDep, user: ActiveUserDep) -> SimulationList:
    return SimulationList(ids=await res.simulations.list_pending(user.user_id))


# Register the static /history path before /{task_id}.
@router.get("/history", responses=errors.to_responses(errors.Unauthorized))
async def simulation_history(res: ResourcesDep, user: ActiveUserDep) -> list[SimulationRecord]:
    return await res.simulations.list_finished(user.user_id)


@router.get(
    "/history/{task_id}", responses=errors.to_responses(errors.Unauthorized, errors.NotFound)
)
async def simulation_history_item(
    task_id: str, res: ResourcesDep, user: ActiveUserDep
) -> SimulationRecord:
    return await res.simulations.get_finished(task_id, user.user_id)


@router.delete(
    "/history/{task_id}", responses=errors.to_responses(errors.Unauthorized, errors.NotFound)
)
async def delete_simulation_history_item(
    task_id: str, res: ResourcesDep, user: ActiveUserDep
) -> None:
    await res.simulations.delete_finished(task_id, user.user_id)


COMMON = (errors.Unauthorized, errors.NotFound)


@router.get("/{task_id}", responses=errors.to_responses(*COMMON))
async def job_status(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> SimulationResult:
    return await res.simulations.get_status(task_id, user.user_id)


@router.delete("/{task_id}", responses=errors.to_responses(*COMMON))
async def abort_job(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> None:
    await res.simulations.abort(task_id, user.user_id)
