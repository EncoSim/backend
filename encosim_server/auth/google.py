import asyncio
from typing import Annotated, Protocol

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

from encosim_server.core import errors
from encosim_server.users.schemas import UserProfile


def string_to_bool(s) -> bool:
    return isinstance(s, str) and s.lower() == "true"


class GoogleVerifier(Protocol):
    async def verify(
        self, id_token: str
    ) -> Annotated[tuple[str, UserProfile], errors.Raises[errors.InvalidGoogleToken]]:
        """Returns (login id, user profile)"""
        ...


class RealGoogleVerifier:
    def __init__(self, client_id: str):
        self.client_id = client_id

    async def verify(
        self, id_token: str
    ) -> Annotated[tuple[str, UserProfile], errors.Raises[errors.InvalidGoogleToken]]:
        """Returns (login id, user profile)"""
        try:
            # google api is blocking
            info = await asyncio.to_thread(
                lambda: g_id_token.verify_oauth2_token(
                    id_token,
                    g_requests.Request(),
                    self.client_id,
                )
            )
        except (ValueError, GoogleAuthError):
            raise errors.InvalidGoogleToken() from None
        profile = UserProfile(
            name=info.get("name"),
            email=info.get("email"),
            email_verified=string_to_bool(info.get("email_verified")),
            picture=info.get("picture"),
        )
        return "google:" + info["sub"], profile


class FakeGoogleVerifier:
    """For test/demo. Token format: 'fake:<sub>:<email>[:<name>]'."""

    async def verify(
        self, id_token: str
    ) -> Annotated[tuple[str, UserProfile], errors.Raises[errors.InvalidGoogleToken]]:
        """Returns (login id, user profile)"""
        parts = id_token.split(":")
        if len(parts) < 3 or parts[0] != "fake":
            raise errors.InvalidGoogleToken()

        profile = UserProfile(
            email=parts[2],
            email_verified=True,
            name=parts[3] if len(parts) > 3 else parts[1],
            picture=None,
        )
        return "google:" + parts[1], profile
