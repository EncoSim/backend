import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from encosim.core.entity_config import create_default

from encosim_server.auth.schema import SessionSchema
from encosim_server.users.schemas import UserProfile
from encosim_server.users.service import UserService

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"


def ids_from_sessions(sessions: list[SessionSchema]) -> set[UUID]:
    return {s.session_id for s in sessions}


async def login(
    users: UserService,
    login_id: str = "google:alice",
    *,
    email: str = "alice@example.com",
    verified: bool = True,
    name: str = "Alice",
):
    """Upsert a user the way the auth layer would after verifying Google."""
    return await users.upsert_login(
        login_id, UserProfile(name=name, email=email, email_verified=verified, picture=None)
    )


def tamper_refresh_token(token: str) -> str:
    """A token for the same session whose secret is wrong (format-agnostic)."""
    return token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")


async def eventually(
    check: Callable[[], Any | Awaitable[Any]],
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    what: str = "condition",
):
    """Poll `check` (sync or async) until it returns something truthy, and return that value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} not met within {timeout}s")
        await asyncio.sleep(interval)


def simulation_payload(
    repetitions: int = 1, team_sizes: tuple[int, ...] = (1, 1)
) -> dict[str, Any]:
    """Request body for POST /simu: one team per entry of `team_sizes`."""
    entity = create_default().model_dump(mode="json")
    return {"repetitions": repetitions, "teams": [[entity] * n for n in team_sizes]}
