from typing import cast
from uuid import UUID, uuid4

import redis
import redis.asyncio as aioredis

from encosim_server.core.settings import Settings
from encosim_server.database import get_redis_kwargs

_ACQUIRE = """
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[2])
return 1
"""


class SimulationLimiter:
    """Enforce per-user concurrent simulation limits using Redis."""

    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs = get_redis_kwargs(settings)
        self._async = aioredis.Redis(**kwargs)
        self._sync = redis.Redis(**kwargs)
        self._acquire = self._async.register_script(_ACQUIRE)

    @property
    def limit(self) -> int:
        return self.settings.max_simulations

    @staticmethod
    def key(user_id: UUID) -> str:
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
        if not sep:
            return None
        try:
            return UUID(hex=uid)
        except ValueError:
            return None

    async def acquire(self, user_id: UUID) -> str | None:
        task_id = self.new_task_id(user_id)
        acquired = await self._acquire(keys=[self.key(user_id)], args=[self.limit, task_id])
        return task_id if acquired else None

    def task_exists(self, task_id: str) -> bool:
        user_id = self.user_id_from_task_id(task_id)
        return bool(user_id and self._sync.sismember(self.key(user_id), task_id))

    def release_sync(self, task_id: str) -> None:
        user_id = self.user_id_from_task_id(task_id)
        if user_id is not None:
            self._sync.srem(self.key(user_id), task_id)

    async def active(self, user_id: UUID) -> set[str]:
        res = await self._async.smembers(self.key(user_id))
        return cast(set[str], res)  # cast is valid, because client decodes responses

    async def close(self) -> None:
        await self._async.aclose()
        self._sync.close()
