"""Fixtures for the service and endpoint tests.

Tests run against a real MongoDB. Connection details come from `.env.test` (copy
`.env.test.example`); set TEST_ENV_FILE to use a different file.

Every test gets its own throw-away database (`<mongo_db>_<random>`) that is dropped afterwards, so
tests are isolated, may run in parallel and never touch your dev data. The MongoDB user therefore
needs the rights to create and drop databases.

Redis is not taken from `.env.test`: the suite starts its own `redis-server` on a private unix
socket. Tests that need celery also get a real worker process, see
`worker`. MongoDB must already be running.
"""

import asyncio
import gc
import logging
import os
import sys
import traceback
from collections.abc import Iterator
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
import redis
from _pytest.monkeypatch import MonkeyPatch
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.core import security
from encosim_server.core.settings import Settings
from encosim_server.database import SESSIONS, connect_mongo, health_check_mongo
from encosim_server.resources import Resources, build_resources
from tests import infra

logger = logging.getLogger(__name__)


# --- time ---------------------------------------------------------------------


class Clock:
    """Controllable replacement for `utcnow`, so ordering/expiry tests are deterministic."""

    def __init__(self):
        self.now = (
            security.utcnow()
        )  # start at the real time (MongoDB's TTL monitor would purge the past)

    def __call__(self):
        return self.now

    def tick(self, **kwargs: Any):
        self.now += timedelta(**kwargs)


@pytest.fixture(scope="function")
def clock(monkeypatch: MonkeyPatch):
    original = security.utcnow
    c = Clock()  # Clock.__init__ calls `utcnow`, so create it before patching
    patched_at_least_one = False
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if name.startswith("encosim_server") and getattr(module, "utcnow", None) is original:
            monkeypatch.setattr(module, "utcnow", c)
            patched_at_least_one = True
    if not patched_at_least_one:
        logger.warning("No clock patch was applied")
    return c


ENV_FILE = Path(os.environ.get("TEST_ENV_FILE", Path(__file__).resolve().parents[1] / ".env.test"))

# Values the tests rely on are pinned here, so they do not depend on the contents of .env.test.
PINNED_SETTINGS = {
    "session_ttl": timedelta(days=30),
    "max_sessions_per_user": 3,
    "auto_promote_to_admin": ["admin@example.com"],
    "cookie_path": "/auth",
    # simulation limits
    "max_simulations": 3,
    "max_simulation_participants": 4,
    "max_simulation_repetitions": 5,
    # celery
    "celery_soft_timeout": timedelta(seconds=2),
    "celery_hard_timeout": timedelta(seconds=4),
}


# --- settings and database ------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_base_settings() -> Settings:
    if not ENV_FILE.is_file():
        raise RuntimeError(f"{ENV_FILE} not found: add .env.test to the directory")
    settings = Settings(_env_file=ENV_FILE)  # type: ignore

    user = settings.mongo_user
    db = settings.mongo_db
    if not ("test" in user.lower() and "test" in db.lower()):
        pytest.exit(
            f"Test execution blocked: MongoDB user {user!r} and database "
            f"{db!r} do not seem to belong to a test environment.",
            returncode=1,
        )
    return settings


def pytest_sessionstart():
    """Fail fast with one clear message instead of a timeout in every single test.

    Only MongoDB is checked: redis and the celery worker are started by the fixtures below.
    """
    try:
        asyncio.run(health_check_mongo(load_base_settings()))
    except Exception as exc:
        traceback.print_exception(exc)
        pytest.exit("Test setup failed", returncode=2)


@pytest.fixture(scope="session")
def redis_server() -> Iterator[infra.RedisServer]:
    """Private redis-server on a unix socket in a temp dir"""
    server = infra.start_redis()
    yield server
    server.stop()


@pytest.fixture(scope="session", autouse=True)
def _finalize_celery_results_before_redis_stops(redis_server: infra.RedisServer) -> Iterator[None]:
    yield
    gc.collect()  # runs before redis_server is torn down


@pytest.fixture(scope="session")
def settings(redis_server: infra.RedisServer) -> Settings:
    update = {**PINNED_SETTINGS, "redis_conn": redis_server.sock}
    return load_base_settings().model_copy(update=update)


@pytest_asyncio.fixture(scope="session")
async def db_session(settings: Settings) -> AsyncIterator[AsyncDatabase[Any]]:
    """database connection that cleans up after session (deep clean)"""
    async with connect_mongo(settings) as database:
        yield database
        for name in await database.list_collection_names():
            await database.drop_collection(name)


@pytest_asyncio.fixture()
async def db(db_session: AsyncDatabase[Any]) -> AsyncIterator[AsyncDatabase[Any]]:
    """database connection that cleans up after every test (shallow/cheap clean)"""
    yield db_session
    names = await db_session.list_collection_names()
    await asyncio.gather(*(db_session[name].delete_many({}) for name in names))


@pytest_asyncio.fixture()
async def disable_ttl_cleanup(db: AsyncDatabase[Any]) -> AsyncIterator[None]:
    """disable timed session cleanup (slow, only use when necessary)"""
    await db[SESSIONS].drop_index("expires_at_1")
    yield
    await db[SESSIONS].create_index("expires_at", expireAfterSeconds=0)


# --- celery ---------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_process(settings: Settings, redis_server: infra.RedisServer) -> Iterator[infra.Worker]:
    """Real celery worker (separate process, prefork pool) on the private redis.

    Started lazily by the first test that asks for `worker`.
    """
    worker = infra.start_worker(settings, ENV_FILE, redis_server.dir / "worker.log")
    yield worker
    worker.stop()


@pytest.fixture
def worker(worker_process: infra.Worker, settings: Settings) -> Iterator[infra.Worker]:
    """Use in every test that submits tasks.

    Afterwards it lets gated tasks finish, waits until the worker is idle (hard timeout plus margin)
    and removes leftover results and limiter entries, so tests cannot influence each other.
    """
    yield worker_process
    infra.open_all_gates(settings.redis_conn)
    worker_process.wait_idle(timeout=settings.celery_hard_timeout.total_seconds() + 10)
    infra.clean_redis(settings.redis_conn)


@pytest.fixture
def redis_client(settings: Settings) -> Iterator[redis.Redis]:
    client = redis.Redis(unix_socket_path=str(settings.redis_conn), decode_responses=True)
    yield client
    client.close()


# --- resources ------------------------------------------------------------------------------------


@pytest.fixture
def resources(db: AsyncDatabase[Any], settings: Settings) -> Resources:
    return build_resources(settings, FakeGoogleVerifier(), db)
