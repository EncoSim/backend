import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.states import READY_STATES
from fastapi.concurrency import run_in_threadpool
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.celery import PatchedCelery
from encosim_server.core import errors
from encosim_server.core.settings import Settings
from encosim_server.database import SIMULATIONS
from encosim_server.simulation.history import finalize_simulation
from encosim_server.simulation.schemas import (
    SimulationConfig,
    SimulationEntry,
    SimulationPendingRecord,
    SimulationRecord,
)
from encosim_server.simulation.tasks import handle_simulation

logger = logging.getLogger(__name__)

FINISHED = ["success", "failed"]


class SimulationManager:
    """Application-facing orchestration for simulation jobs and their finished history.

    Celery manages live task state. MongoDB stores the submitted configuration and the durable,
    user-facing outcome.
    """

    def __init__(self, settings: Settings, db: AsyncDatabase[Any], celery: PatchedCelery):
        self.settings = settings
        self.celery = celery
        self.limiter = celery.simulation_limiter
        self.records = db[SIMULATIONS]

    @staticmethod
    async def _submit(config: SimulationConfig, task_id: str):
        return await run_in_threadpool(
            cast(Task, handle_simulation).apply_async,
            kwargs={"config": config.model_dump(mode="json")},
            task_id=task_id,
        )

    async def submit(
        self, config: SimulationConfig, owner_id: UUID
    ) -> Annotated[SimulationEntry, errors.Raises[errors.TooManySimulations]]:
        # Reap completed jobs whose worker was killed before its task-finally hooks ran.
        await self.list_pending(owner_id)

        task_id = await self.limiter.acquire(owner_id)
        if task_id is None:
            raise errors.TooManySimulations(
                detail=errors.AllowedNumberDetail(allowed=self.settings.max_simulations)
            )

        now = datetime.now(timezone.utc)
        try:
            await self.records.insert_one(
                {
                    "_id": task_id,
                    "owner_id": owner_id,
                    "config": config.model_dump(mode="json"),
                    "submitted_at": now,
                }
            )
            result = await self._submit(config, task_id)
        except Exception:
            try:
                await self.records.delete_one({"_id": task_id, "owner_id": owner_id})
            except Exception:
                logger.exception("Could not remove unpublished simulation %s", task_id)
            finally:
                self.limiter.release_sync(task_id)
            raise

        try:
            await run_in_threadpool(result.get, timeout=15, propagate=False)
        except Exception as exc:
            # A Celery timeout means the task is still pending; broker/backend errors should
            # not be mistaken for a simulation failure.
            from celery.exceptions import TimeoutError as CeleryTimeout

            if not isinstance(exc, CeleryTimeout):
                logger.exception("Could not wait for simulation %s", task_id)
        return await self.get(task_id, owner_id)

    async def list_pending(self, owner_id: UUID) -> list[str]:
        """list pending tasks and clean up stale entries"""
        active = await self.limiter.active(owner_id)
        pending: list[str] = []
        for task_id in sorted(active):
            if not self.limiter.task_belongs_to(task_id, owner_id):
                self.limiter.release_sync(task_id)
                continue
            try:
                entry = await self.get(task_id, owner_id)
            except errors.NotFound:
                self.limiter.release_sync(task_id)
                continue
            if entry.status == "pending":
                pending.append(task_id)
        return pending

    async def get(
        self, task_id: str, owner_id: UUID
    ) -> Annotated[SimulationEntry, errors.Raises[errors.NotFound]]:
        """A single simulation of the user, pending or finished, including its configuration."""
        if not self.limiter.task_belongs_to(task_id, owner_id):
            raise errors.NotFound()
        doc = await self.records.find_one({"_id": task_id, "owner_id": owner_id})
        if doc is None:
            raise errors.NotFound()

        if doc.get("status") in FINISHED:
            self.limiter.release_sync(task_id)
            return SimulationRecord.model_validate(doc)

        result = self.celery.AsyncResult(task_id)
        state, value, info = await run_in_threadpool(
            lambda: (result.state, result.result, result.info)
        )
        if state not in READY_STATES:
            # The task may have committed its history just before Celery stores its result.
            fresh = await self.records.find_one({"_id": task_id, "owner_id": owner_id})
            if fresh and fresh.get("status") in FINISHED:
                self.limiter.release_sync(task_id)
                return SimulationRecord.model_validate(fresh)
            if not self.limiter.task_exists(task_id):
                raise errors.NotFound()
            return SimulationPendingRecord.model_validate(doc)

        # A terminal Celery result can be observed before/without the task's history hook
        # (e.g. a worker process killed by a hard time limit). Backfill the domain record.
        await self._persist_backend_outcome(task_id, owner_id, state, value, info)
        self.limiter.release_sync(task_id)
        fresh = await self.records.find_one({"_id": task_id, "owner_id": owner_id})
        if fresh and fresh.get("status") in FINISHED:
            return SimulationRecord.model_validate(fresh)
        # The outcome could not be written to the record; report it anyway rather than
        # claiming the simulation is still pending.
        status, summary, error_type = self._outcome(state, value, info)
        return SimulationRecord.model_validate(
            {
                **doc,
                "status": status,
                "summary": summary,
                "error_type": error_type,
                "finished_at": datetime.now(timezone.utc),
            }
        )

    @staticmethod
    def _outcome(
        state: str, value: Any, info: Any
    ) -> tuple[Literal["success", "failed"], str, str | None]:
        """Status, summary and error type for a terminal Celery state."""
        if state == "SUCCESS":
            return "success", str(value), None
        summary = (
            "simulation timed out"
            if isinstance(info, (TimeLimitExceeded, SoftTimeLimitExceeded))
            else "simulation aborted"
            if state == "REVOKED"
            else "critical failure"
        )
        return "failed", summary, type(info).__name__ if info is not None else state

    async def _persist_backend_outcome(
        self,
        task_id: str,
        owner_id: UUID,
        state: str,
        value: Any,
        info: Any,
    ) -> None:
        status, summary, error_type = self._outcome(state, value, info)
        await run_in_threadpool(
            finalize_simulation,
            self.celery.backend,
            task_id,
            owner_id,
            status=status,
            summary=summary,
            error_type=error_type,
            limit=self.settings.max_finished_simulations_per_user,
        )

    async def abort(
        self, task_id: str, owner_id: UUID
    ) -> Annotated[None, errors.Raises[errors.NotFound]]:
        if not self.limiter.task_belongs_to(task_id, owner_id):
            raise errors.NotFound()
        doc = await self.records.find_one({"_id": task_id, "owner_id": owner_id})
        if doc is None or doc.get("status") in ("success", "failed"):
            raise errors.NotFound()
        if not self.limiter.task_exists(task_id):
            raise errors.NotFound()

        result = self.celery.AsyncResult(task_id)
        state = await run_in_threadpool(lambda: result.state)
        if state in READY_STATES:
            await self.get(task_id, owner_id)
            raise errors.NotFound()

        # Claim the terminal history outcome before broadcasting revoke. This prevents a
        # worker's failure hook (triggered by SIGKILL) from recording a generic failure first.
        written = await run_in_threadpool(
            finalize_simulation,
            self.celery.backend,
            task_id,
            owner_id,
            status="failed",
            summary="simulation aborted",
            error_type="TaskRevokedError",
            limit=self.settings.max_finished_simulations_per_user,
        )
        if not written:
            current = await self.records.find_one({"_id": task_id, "owner_id": owner_id})
            if current and current.get("summary") == "simulation aborted":
                self.limiter.release_sync(task_id)
                return
            raise errors.NotFound()

        try:
            await run_in_threadpool(result.revoke, terminate=True, signal="SIGKILL")
        except Exception:
            # If the control broadcast failed, allow a later worker outcome to be recorded.
            await self.records.update_one(
                {
                    "_id": task_id,
                    "owner_id": owner_id,
                    "status": "failed",
                    "error_type": "TaskRevokedError",
                },
                {
                    "$unset": {
                        "status": "",
                        "result": "",
                        "summary": "",
                        "error_type": "",
                        "finished_at": "",
                    }
                },
            )
            raise
        self.limiter.release_sync(task_id)

    async def list_all(self, owner_id: UUID) -> list[SimulationEntry]:
        """All simulations of the user, pending and finished, newest submission first."""
        # Reconcile terminal Celery states before exposing the durable records.
        pending_ids = set(await self.list_pending(owner_id))
        entries: list[SimulationEntry] = []
        cursor = self.records.find({"owner_id": owner_id}).sort([("submitted_at", -1), ("_id", -1)])
        async with cursor:
            async for doc in cursor:
                if doc.get("status") in FINISHED:
                    entries.append(SimulationRecord.model_validate(doc))
                elif doc["_id"] in pending_ids:
                    entries.append(SimulationPendingRecord.model_validate(doc))
                # else: a record without a live task (e.g. a crashed submit); not listed
        return entries

    async def delete_finished(
        self, task_id: str, owner_id: UUID
    ) -> Annotated[None, errors.Raises[errors.NotFound]]:
        await self.get(task_id, owner_id)  # reconcile; NotFound if unknown
        deleted = await self.records.delete_one(
            {"_id": task_id, "owner_id": owner_id, "status": {"$in": FINISHED}}
        )
        if not deleted.deleted_count:
            raise errors.NotFound()
        try:
            await run_in_threadpool(self.celery.backend.forget, task_id)  # type: ignore
        except Exception:
            logger.exception("Could not remove Celery result for deleted simulation %s", task_id)
