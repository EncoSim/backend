import asyncio

from encosim import run_full_stat_recap_from_configs
from fastapi import APIRouter

from encosim_server.core import errors
from encosim_server.depends import OptionalUserDep, ResourcesDep
from encosim_server.simulation.schemas import SimulatonSchema

router = APIRouter(prefix="/simu", tags=["simu"])


@router.post("")
async def simulate(res: ResourcesDep, maybe_user: OptionalUserDep, battle: SimulatonSchema) -> str:
    # logged in users have more priviledges
    # TODO: add more priviledges
    max_reps = res.settings.max_simulation_repetitions_anon
    max_part = res.settings.max_simulation_participants_anon
    if maybe_user is not None:
        max_reps = res.settings.max_simulation_repetitions
        max_part = res.settings.max_simulation_participants

    if battle.repetitions > max_reps:
        raise errors.TooManyRepetitions(detail=errors.AllowedNumberDetail(allowed=max_reps))

    total_members = sum(len(t) for t in battle.teams)
    if total_members > max_part:
        raise errors.TooManyRepetitions(detail=errors.AllowedNumberDetail(allowed=max_part))

    # TODO: delegate work properly
    async def work() -> str:
        return run_full_stat_recap_from_configs(repetitions=battle.repetitions, teams=battle.teams)

    summary = await asyncio.wait_for(work(), timeout=res.settings.simulation_timeout)
    return summary
