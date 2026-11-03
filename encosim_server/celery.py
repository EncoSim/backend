import sys

from celery import Celery, Task

from encosim_server.core.settings import Settings
from encosim_server.database import make_mongo_backend_settings, make_redis_uri
from encosim_server.simulation.limiter import SimulationLimiter


class PatchedCelery(Celery):
    simulation_limiter: SimulationLimiter

    def close_backend(self) -> None:
        """Close the synchronous Mongo client if this process opened one."""
        connection = getattr(self.backend, "_connection", None)
        if connection is not None:
            connection.close()


def create_celery(settings: Settings) -> PatchedCelery:
    app = PatchedCelery(
        "encosim",
        broker=make_redis_uri(settings),
        backend="mongodb://",
        include=["encosim_server.simulation.tasks"],
    )
    app.conf.update(
        mongodb_backend_settings=make_mongo_backend_settings(settings),
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_track_started=True,
        task_soft_time_limit=int(settings.celery_soft_timeout.total_seconds()),
        task_time_limit=int(settings.celery_hard_timeout.total_seconds()),
        # History retention/deletion explicitly forgets old Celery result records.
        result_expires=None,
    )
    app.simulation_limiter = SimulationLimiter(settings)
    return app


class LimitedTask(Task):
    """Base task that idempotently releases a reserved per-user simulation slot."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.app.simulation_limiter.release_sync(task_id)
        return super().on_failure(exc, task_id, args, kwargs, einfo)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        if status != "RETRY":
            self.app.simulation_limiter.release_sync(task_id)
        return super().after_return(status, retval, task_id, args, kwargs, einfo)


def main():
    """Keep `python -m encosim_server.celery` and the CLI worker entry point working."""
    from encosim_server.cli import main as cli_main

    raise SystemExit(cli_main(["worker", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
