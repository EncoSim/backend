# --- refresh cookie --------------------------------------------------------

from typing import Annotated

from fastapi import Cookie, Request, Response

from encosim_server.core.settings import Settings

REFRESH_COOKIE_NAME: str = "Refresh-Token"
REFRESH_COOKIE_PATH: str = "/auth"

REFRESH_COOKIE_HEADER = {
    "Set-Cookie": {
        "description": "Sets the refresh-token cookie.",
        "schema": {"type": "string"},
    }
}
RefreshToken = Annotated[
    str | None,
    Cookie(
        alias=REFRESH_COOKIE_NAME,
        description="Holds the current refresh-token of the login session",
    ),
]


def resolve_refresh_cookie_path(request: Request) -> str:
    return request.scope.get("root_path", "") + REFRESH_COOKIE_PATH


def set_refresh_cookie(
    request: Request, response: Response, settings: Settings, token: str
) -> None:
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=int(settings.session_ttl.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=resolve_refresh_cookie_path(request),
    )


def clear_refresh_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path=resolve_refresh_cookie_path(request))
