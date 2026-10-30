import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import pymongo
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError
from redis import RedisError
from redis.asyncio import Redis

from encosim_server.core.settings import Settings

logger = logging.getLogger(__name__)

CHARACTERS = "characters"
USERS = "users"
SESSIONS = "session"
TOMBSTONES = "tombstones"

def make_redis_uri(settings: Settings) -> str:
    # celery needs special format
    return f"redis+socket://{settings.redis_conn}"


async def health_check_redis(settings: Settings, timeout: float = 5):
    redis = Redis(
        unix_socket_path=str(settings.redis_conn),
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    try:
        if not await redis.ping():
            raise RuntimeError("Could not connect to Redis")
    except RedisError as e:
        raise RuntimeError(f"Could not connect to Redis:\n{e}") from e
    finally:
        await redis.aclose()


def make_mongo_uri(settings: Settings) -> str:
    user = settings.mongo_user
    pw = settings.mongo_pass.get_secret_value()
    sock = quote_plus(str(settings.mongo_conn))
    db = settings.mongo_db
    rs = settings.mongo_rs
    port = 9743
    return f"mongodb://{user}:{pw}@{sock}/{db}?replicaSet={rs}&authSource=admin"


async def create_indexes(db: AsyncDatabase[Any]) -> None:
    # TODO: easier way to keep in sync?
    # should very rarely change outside of early development
    await db[CHARACTERS].create_index("owner_id")
    await db[USERS].create_index("login_id", unique=True)
    await db[SESSIONS].create_index("user_id")
    # auto delete expired sessions
    await db[SESSIONS].create_index("expires_at", expireAfterSeconds=0)


async def health_check_mongo(settings: Settings, timeout: float = 5):
    try:
        with pymongo.timeout(timeout):
            async with connect_mongo(settings) as db:
                res = await db.command("ping")
                if res.get("ok") == 1.0:
                    logger.info("Connected to MongoDB", res)
                    return
                else:
                    raise RuntimeError(f"Unexpected ping response from MongoDB: {res}")
    except PyMongoError as e:
        raise RuntimeError(f"Could not connect to MongoDB:\n{e}") from e


@asynccontextmanager
async def connect_mongo(settings: Settings):
    client = AsyncMongoClient(
        host=make_mongo_uri(settings),
        tz_aware=True,
        uuidRepresentation="standard",
    )
    try:
        db = client[settings.mongo_db]
        await create_indexes(db)
        yield db
    finally:
        await client.close()
