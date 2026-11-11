from fastapi import APIRouter

from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep
from encosim_server.simulation.manager import SimulationManager
from encosim_server.simulation.schemas import SimulationConfig, SimulationEntry

router = APIRouter(prefix="/simulation", tags=["simulations"])

COMMON = (errors.Unauthorized,)


@router.post(
    "",
    status_code=202,
    responses=errors.to_responses_from(
        SimulationManager.submit,
        extra=(*COMMON, errors.TooManyParticipants, errors.TooManyRepetitions),
    ),
)
async def simulate(
    res: ResourcesDep, user: ActiveUserDep, config: SimulationConfig
) -> SimulationEntry:
    """Start a simulation. Short simulations are answered with their result directly,
    otherwise the response is `pending` and the simulation can be polled by its `task_id`."""
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


@router.get("", responses=errors.to_responses(*COMMON))
async def list_simulations(res: ResourcesDep, user: ActiveUserDep) -> list[SimulationEntry]:
    """All simulations of the user, pending and finished, newest first."""
    return await res.simulations.list_all(user.user_id)


@router.get(
    "/{task_id}",
    responses=errors.to_responses_from(SimulationManager.get, extra=COMMON),
)
async def get_simulation(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> SimulationEntry:
    """A single simulation, pending or finished."""
    return await res.simulations.get(task_id, user.user_id)


@router.post(
    "/{task_id}/abort",
    responses=errors.to_responses_from(SimulationManager.abort, extra=COMMON),
)
async def abort_simulation(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> None:
    """Abort a pending simulation. It stays in the list as a failed (aborted) simulation."""
    await res.simulations.abort(task_id, user.user_id)


@router.delete(
    "/{task_id}",
    responses=errors.to_responses_from(SimulationManager.delete_finished, extra=COMMON),
)
async def delete_simulation(task_id: str, res: ResourcesDep, user: ActiveUserDep) -> None:
    """Delete the record of a finished simulation. Pending simulations have to be aborted first."""
    await res.simulations.delete_finished(task_id, user.user_id)
