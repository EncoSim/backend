import logging
from typing import Any

from celery import Task, shared_task, states
from celery.exceptions import (
    Retry,
    SoftTimeLimitExceeded,
    TaskRevokedError,
    TimeLimitExceeded,
)
from celery.signals import task_failure
from celery.worker.request import Request
from encosim.api_endpoint import run_full_stat_recap_from_configs

from encosim_server.celery import LimitedTask
from encosim_server.simulation.history import finalize_simulation
from encosim_server.simulation.limiter import SimulationLimiter
from encosim_server.simulation.schemas import SimulationConfig

logger = logging.getLogger(__name__)


class SimulationRequest(Request):
    """Persist hard timeouts handled by Celery's parent worker process."""

    def on_timeout(self, soft: bool, timeout: float) -> None:
        super().on_timeout(soft, timeout)
        if not soft:
            try:
                failed = self.task.backend.get_state(self.id) == states.FAILURE
            except Exception:
                logger.exception("Could not inspect hard timeout for simulation %s", self.id)
                return
            if failed:
                try:
                    self.task.persist_outcome(self.id, "failed", error=TimeLimitExceeded(timeout))
                except Exception:
                    logger.exception("Could not persist hard timeout for simulation %s", self.id)
                finally:
                    self.task.app.simulation_limiter.release_sync(self.id)


class SimulationTask(LimitedTask):
    """Persist the domain outcome while Celery remains responsible for task state."""

    Request = SimulationRequest  # type: ignore - celery is not typed

    def __call__(self, *args: Any, **kwargs: Any):
        task_id = self.request.id
        try:
            result = super().__call__(*args, **kwargs)
        except Retry:
            raise
        except Exception as exc:
            self.persist_outcome(task_id, "failed", error=exc)
            raise
        else:
            self.persist_outcome(task_id, "success", result=result)
            return result

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Fallback for hard time limits/worker loss, where __call__ never catches the error.
        try:
            self.persist_outcome(task_id, "failed", error=exc)
        except Exception:
            logger.exception("Could not persist failed simulation %s", task_id)
        return super().on_failure(exc, task_id, args, kwargs, einfo)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        # Idempotent safety net for Celery's worker trace path.
        try:
            if status == "SUCCESS":
                self.persist_outcome(task_id, "success", result=retval)
            elif status == "FAILURE":
                self.persist_outcome(task_id, "failed", error=retval)
        except Exception:
            logger.exception("Could not persist terminal simulation %s", task_id)
        return super().after_return(status, retval, task_id, args, kwargs, einfo)

    def persist_outcome(
        self,
        task_id: str | None,
        status: str,
        *,
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        if not task_id:
            return
        owner_id = SimulationLimiter.user_id_from_task_id(task_id)
        if owner_id is None:
            return

        if status == "success":
            summary = str(result)
            error_type = None
            value = result
        else:
            summary = (
                "simulation timed out"
                if isinstance(error, (SoftTimeLimitExceeded, TimeLimitExceeded))
                else "simulation aborted"
                if isinstance(error, TaskRevokedError)
                else "critical failure"
            )
            error_type = type(error).__name__ if error is not None else "UnknownError"
            value = None

        finalize_simulation(
            self.app.backend,
            task_id,
            owner_id,
            status=status,
            result=value,
            summary=summary,
            error_type=error_type,
            limit=self.app.simulation_limiter.settings.max_finished_simulations_per_user,
        )


@task_failure.connect(weak=False, dispatch_uid="encosim-simulation-history-failure")
def persist_failed_simulation(
    sender: Task | None = None,
    task_id: str | None = None,
    exception: Exception | None = None,
    **_kwargs,
):
    """Cover worker-side failures reported by Celery's parent process."""
    persist = getattr(sender, "persist_outcome", None)
    if callable(persist) and task_id and sender:
        try:
            persist(task_id, "failed", error=exception)
        except Exception:
            logger.exception("Could not persist failed simulation %s", task_id)
        finally:
            sender.app.simulation_limiter.release_sync(task_id)


@shared_task(base=SimulationTask, bind=True, pydantic=True)
def handle_simulation(self, config: SimulationConfig) -> str:
    return run_full_stat_recap_from_configs(repetitions=config.repetitions, teams=config.teams)
