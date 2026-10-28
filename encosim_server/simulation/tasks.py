from uuid import UUID

from celery import shared_task
from encosim.api_endpoint import run_full_stat_recap_from_configs

from encosim_server.celery import LimitedTask
from encosim_server.simulation.schemas import SimulatonConfig


@shared_task(base=LimitedTask, bind=True, pydantic=True, soft_time_limit=300, time_limit=360)
def handle_simulation(self, config: SimulatonConfig) -> str:
    summary = run_full_stat_recap_from_configs(repetitions=config.repetitions, teams=config.teams)
    return summary
