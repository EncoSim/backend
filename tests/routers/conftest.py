from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.app import create_app
from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources
from encosim_server.users.models import UserInDB
from encosim_server.users.schemas import UserProfile
from tests.helpers import CHROME_WIN

# "https" so cookies flagged `Secure` are sent back by httpx
# the app runs with root_path="/api"
BASE_URL = "https://localhost/api"


@pytest_asyncio.fixture
async def app(settings: Settings, db: AsyncDatabase[Any]):
    # fastapi and fixtures connect to same mongod server.
    # depending on the db fixture here cleans up the database for the next test.
    # `pytest.mark.usefixtures` is not allowed on fixtures
    _ = db
    application = create_app(settings, google=FakeGoogleVerifier())
    async with LifespanManager(application) as manager:
        yield manager.app


MakeClient = Callable[..., httpx.AsyncClient]


@pytest_asyncio.fixture
async def make_client(app: FastAPI) -> AsyncIterator[MakeClient]:
    """httpx client factory (so cookies dont spill)"""
    clients: list[httpx.AsyncClient] = []

    def factory(**kwargs: Any) -> httpx.AsyncClient:
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
async def client(make_client: Callable[..., httpx.AsyncClient]) -> httpx.AsyncClient:
    """Anonymous client."""
    return make_client()


@dataclass
class LoggedIn:
    client: httpx.AsyncClient  # sends the access token and holds the refresh cookie
    user: UserInDB
    session_id: UUID
    access_token: str
    refresh_token: str


LoginAs = Callable[..., Awaitable[LoggedIn]]


@pytest_asyncio.fixture
async def login_as(resources: Resources, make_client: Callable[..., httpx.AsyncClient]) -> LoginAs:
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
        user, _ = await resources.users.upsert_login(login_id, profile)
        await resources.users.set_admin(user.user_id, is_admin)

        refresh_token, session_id = await resources.sessions.create(user, user_agent)
        access_token = create_access_token(
            resources.settings,
            user_id=user.user_id,
            session_id=session_id,
        )

        client = make_client(headers={"Authorization": f"Bearer {access_token}"})
        client.cookies.set(
            resources.settings.cookie_name,
            refresh_token,
            domain="localhost",
            path="/api" + resources.settings.cookie_path,
        )
        return LoggedIn(client, user, session_id, access_token, refresh_token)

    return _login
