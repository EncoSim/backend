from typing import cast
from uuid import UUID, uuid4

import redis
import redis.asyncio as aioredis
from celery import Celery, Task
from celery.result import AsyncResult
from celery.signals import setup_logging

from encosim_server.core.settings import Settings, get_settings
from encosim_server.database import make_redis_uri
from encosim_server.logs import configure_logging

# TODO: make task limiter a service and wrap celery with it


class PatchedCelery(Celery):
    user_limiter: "UserTaskLimiter"
    """Use this as general interface when delegating work to celery"""


def create_celery(settings: Settings) -> PatchedCelery:
    uri = make_redis_uri(settings)
    app = PatchedCelery(
        "encosim",
        broker=uri,
        backend=uri,
        include=["encosim_server.simulation.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_soft_time_limit=int(settings.celery_soft_timeout.total_seconds()),
        task_hard_time_limit=int(settings.celery_hard_timeout.total_seconds()),
        result_extended=True,
    )

    @setup_logging.connect(weak=False, dispatch_uid="encosim-logging")
    def _configure_logging(**_):
        configure_logging(
            "celery",
            logs_dir=settings.logs_dir,
            level=settings.log_level,
            celery_tasks=True,
        )

    app.user_limiter = UserTaskLimiter(settings)
    return app


# Atomic "check size, then add". KEYS[1]=set key, ARGV[1]=limit, ARGV[2]=task id
_ACQUIRE = """
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[2])
return 1
"""


class UserTaskLimiter:
    """Manage limitation of user resources in celery"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._async = aioredis.Redis(
            unix_socket_path=str(settings.redis_conn),
            decode_responses=True,
        )
        self._sync = redis.Redis(
            unix_socket_path=str(settings.redis_conn),
            decode_responses=True,
        )
        self._acquire = self._async.register_script(_ACQUIRE)

    @property
    def limit(self) -> int:
        return self.settings.max_simulations

    @staticmethod
    def _key(user_id: UUID) -> str:
        return f"limits:user:{user_id.hex}:tasks"

    @staticmethod
    def new_task_id(user_id: UUID) -> str:
        return f"{user_id.hex}+{uuid4().hex}"

    @staticmethod
    def task_belongs_to(task_id: str, user_id: UUID) -> bool:
        return task_id.startswith(f"{user_id.hex}+")

    @staticmethod
    def user_id_from_task_id(task_id: str) -> UUID | None:
        uid, sep, _ = task_id.partition("+")
        if sep == "":
            return
        return UUID(hex=uid)

    def task_exists(self, task_id: str) -> bool:
        user_id = self.user_id_from_task_id(task_id)
        if user_id is None:
            return False
        return bool(self._sync.sismember(self._key(user_id), task_id))

    async def submit(self, task, user_id: UUID, **kwargs) -> AsyncResult | None:
        """Enqueue a task. Returns the AsyncResult, or None if at the limit."""
        task = cast(Task, task)  # real task is a proxy for Task
        task_id = self.new_task_id(user_id)
        key = self._key(user_id)
        if not await self._acquire(keys=[key], args=[self.limit, task_id]):
            return None
        try:
            return task.apply_async(kwargs=kwargs, task_id=task_id)
        except Exception:
            await self._async.srem(key, task_id)  # message never reached the broker
            raise

    def release_sync(self, task_id: str):
        user_id = UserTaskLimiter.user_id_from_task_id(task_id)
        if user_id is None:
            return
        self._sync.srem(self._key(user_id), task_id)

    # --- for debugging / cleanup ---
    async def active(self, user_id: UUID):
        # configured with `decode_responses=True`
        return cast(set[str], await self._async.smembers(self._key(user_id)))


class LimitedTask(Task):
    """Base class for limited tasks. Releases via the limiter attached to the app."""

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        if status == "RETRY":  # still occupying its slot
            return
        self.app.user_limiter.release_sync(task_id)


def main():
    settings = get_settings()
    app = create_celery(settings)
    app.worker_main(["worker", f"--loglevel={settings.log_level}"])
    return app


if __name__ == "__main__":
    main()
