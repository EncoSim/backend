"""Fixtures for the service and endpoint tests.

Tests run against a real MongoDB. Connection details come from `.env.test`
(copy `.env.test.example`); set TEST_ENV_FILE to use a different file.

Every test gets its own throw-away database (`<mongo_db>_<random>`) that is dropped afterwards,
so tests are isolated, may run in parallel and never touch your dev data.
The MongoDB user therefore needs the rights to create and drop databases.

DRAFT: not run yet. Places that depend on code I have not seen are marked `ADAPT`.
"""

import asyncio
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.core import security
from encosim_server.core.settings import Settings
from encosim_server.database import (
    CHARACTERS,
    SESSIONS,
    TOMBSTONES,
    USERS,
    connect_mongo,
)
from encosim_server.main import preflight
from encosim_server.resources import Resources, build_resources
from encosim_server.services.sessions import SessionService
from encosim_server.services.users import UserInDB, UserProfile, UserService
from tests.helpers import CHROME_WIN

ENV_FILE = Path(os.environ.get("TEST_ENV_FILE", Path(__file__).resolve().parents[1] / ".env.test"))

# "https" so cookies flagged `Secure` are sent back by httpx; "localhost" is a host name
# the cookie jar accepts (a bare "test" is not).
BASE_URL = "https://localhost"

# Values the tests rely on are pinned here, so they do not depend on the contents of .env.test.
PINNED_SETTINGS = {
    "session_ttl": timedelta(days=30),
    "max_sessions_per_user": 3,
    "auto_promote_to_admin": ["admin@example.com"],
}


# --- settings and database -----------------------------------------------------------------


@lru_cache
def load_base_settings() -> Settings:
    if not ENV_FILE.is_file():
        raise RuntimeError(
            f"{ENV_FILE} not found: copy .env.test.example to .env.test and fill it in"
        )
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
    """Fail fast with one clear message instead of a timeout in every single test."""
    try:
        asyncio.run(preflight(load_base_settings()))
    except Exception as exc:
        traceback.print_exception(exc)
        pytest.exit("Test setup failed", returncode=2)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_base_settings().model_copy(update=PINNED_SETTINGS)


@pytest_asyncio.fixture(scope="session")
async def db(settings):
    async with connect_mongo(settings) as database:
        yield database
        # full clean
        for name in await database.list_collection_names():
            await database.drop_collection(name)


COLLECTIONS = (CHARACTERS, USERS, SESSIONS, TOMBSTONES)


@pytest_asyncio.fixture(autouse=True, scope="function")
async def clean_db(db):
    await asyncio.gather(*(db[name].delete_many({}) for name in COLLECTIONS))


@pytest_asyncio.fixture(scope="function")
async def disable_ttl_cleanup(db):
    await db[SESSIONS].drop_index("expires_at_1")
    yield
    await db[SESSIONS].create_index("expires_at", expireAfterSeconds=0)


# --- time ------------------------------------------------------------------------------------


class Clock:
    """Controllable replacement for `utcnow`, so ordering/expiry tests are deterministic."""

    def __init__(self):
        self.now = (
            security.utcnow()
        )  # start at the real time (MongoDB's TTL monitor would purge the past)

    def __call__(self):
        return self.now

    def tick(self, **kwargs):
        self.now += timedelta(**kwargs)


logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def clock(monkeypatch):
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


# --- services ------------------------------------------------------------------------------------


@pytest.fixture
def resources(db, settings) -> Resources:
    return build_resources(settings, FakeGoogleVerifier(), db)


@pytest.fixture
def users(resources: Resources) -> UserService:
    return resources.users


@pytest.fixture
def sessions(resources: Resources) -> SessionService:
    return resources.sessions


# --- endpoints (httpx) ----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app(settings, db):
    from encosim_server.main import create_app

    application = create_app(settings, google=FakeGoogleVerifier())
    async with application.router.lifespan_context(application):  # runs startup/shutdown
        yield application


@pytest_asyncio.fixture
async def make_client(app):
    """httpx client factory (so cookies dont spill)"""
    clients: list[httpx.AsyncClient] = []

    def factory(**kwargs) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            **kwargs,
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


@pytest_asyncio.fixture
async def client(make_client):
    """Anonymous client."""
    return make_client()


@dataclass
class LoggedIn:
    client: httpx.AsyncClient  # sends the access token and holds the refresh cookie
    user: UserInDB
    session_id: UUID
    access_token: str
    refresh_token: str


@pytest_asyncio.fixture
async def login_as(users: UserService, sessions: SessionService, settings: Settings, make_client):
    """Authenticated clients without going through Google.

    alice = await login_as("google:alice")
    admin = await login_as("google:root", roles=["user", "admin"])
    r = await alice.client.get("/me")
    """
    from encosim_server.core.security import create_access_token  # ADAPT: name/location

    async def _login(
        login_id: str = "google:alice",
        *,
        is_admin: bool = False,
        user_agent: str = CHROME_WIN,
    ) -> LoggedIn:
        name = login_id.split(":")[-1]
        profile = UserProfile(
            name=name, email=f"{name}@example.com", email_verified=True, picture=None
        )
        user = await users.upsert_login(login_id, profile)
        await users.set_admin(user.user_id, is_admin)

        refresh_token, session_id = await sessions.create(user, user_agent)
        access_token = create_access_token(settings, user_id=user.user_id, session_id=session_id)

        client = cast(
            httpx.AsyncClient,
            make_client(headers={"Authorization": f"Bearer {access_token}"}),
        )
        client.cookies.set(settings.cookie_name, refresh_token, domain="localhost", path="/auth")
        return LoggedIn(client, user, session_id, access_token, refresh_token)

    return _login
