from uuid import UUID

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
    login_id="google:alice",
    *,
    email="alice@example.com",
    verified=True,
    name="Alice",
):
    """Upsert a user the way the auth layer would after verifying Google."""
    return await users.upsert_login(
        login_id, UserProfile(name=name, email=email, email_verified=verified, picture=None)
    )


def tamper_refresh_token(token: str) -> str:
    """A token for the same session whose secret is wrong (format-agnostic)."""
    return token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
