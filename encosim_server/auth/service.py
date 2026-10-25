from dataclasses import dataclass
from uuid import UUID

from encosim_server.auth.exceptions import INVALID_SESSION
from encosim_server.auth.google import GoogleVerifier
from encosim_server.auth.sessions import SessionService
from encosim_server.core.security import create_access_token
from encosim_server.core.settings import Settings
from encosim_server.users.models import UserInDB
from encosim_server.users.service import UserService


@dataclass
class LoginResult:
    user: UserInDB
    access_token: str
    refresh_token: str


class AuthService:
    def __init__(
        self,
        settings: Settings,
        google: GoogleVerifier,
        sessions: SessionService,
        users: UserService,
    ):
        self.settings = settings
        self.google = google
        self.users = users
        self.sessions = sessions

    def _new_access(self, user: UserInDB, session_id: UUID) -> str:
        return create_access_token(self.settings, user_id=user.user_id, session_id=session_id)

    async def login_google(self, id_token: str, user_agent: str | None) -> LoginResult:
        id, profile = await self.google.verify(id_token)
        user = await self.users.upsert_login(id, profile)
        refresh, sid = await self.sessions.create(user, user_agent)
        return LoginResult(user, self._new_access(user, sid), refresh)

    async def refresh(self, refresh_token: str) -> LoginResult:
        """refresh the login session"""
        new_refresh, session_id, user_id = await self.sessions.rotate(refresh_token)
        user = await self.users.get(user_id)
        if not user or user.ban_status.banned:
            await self.sessions.terminate_all_for_user(user_id)
            raise INVALID_SESSION
        return LoginResult(user, self._new_access(user, session_id), new_refresh)

    async def logout(self, refresh_token: str | None) -> None:
        """Silent on failure. Uses the refresh cookie so it works with an expired access token."""
        if refresh_token:
            await self.sessions.terminate_by_token(refresh_token)
