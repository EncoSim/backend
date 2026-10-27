from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi_typed_errors import Raises, with_errors
from pydantic import UUID4

from encosim_server.auth.schema import (
    GoogleLoginInSchema,
    LoginOutSchema,
    ManySessionsSchema,
)
from encosim_server.core import exceptions
from encosim_server.core.settings import Settings
from encosim_server.depends import ActiveUserDep, ResourcesDep

router = with_errors(APIRouter(prefix="/auth", tags=["auth"]), auto=True)

def refresh_cookie_path(request: Request) -> str:
    """The path the BROWSER sees. Behind `root_path="/api"` that is /api/auth, not /auth: with the
    wrong path the browser never sends the cookie back and every refresh fails."""
    return f"{request.scope.get('root_path', '')}{router.prefix}"


def set_refresh_cookie(
    request: Request, response: Response, settings: Settings, token: str
) -> None:
    response.set_cookie(
        settings.cookie_name,  # ADAPT: cookie_* field names
        token,
        max_age=int(settings.session_ttl.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=refresh_cookie_path(request),
    )


def clear_refresh_cookie(request: Request, response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.cookie_name, path=refresh_cookie_path(request))


@router.post(
    "/login/google",
    response_model=LoginOutSchema,
    responses={201: {"model": LoginOutSchema}},
    description="login with google (sets session cookie)",
)
async def login_google(
    res: ResourcesDep, token: GoogleLoginInSchema, request: Request
) -> Annotated[Response, Raises[exceptions.InvalidGoogleToken, exceptions.Banned]]:
    login_result = await res.auth.login_google(token.id_token, request.headers.get("user-agent"))
    out = LoginOutSchema(
        user_id=login_result.user.user_id,
        profile=login_result.user.profile,
        access_token=login_result.access_token,
    )
    response = Response(
        out.model_dump_json(),
        media_type="content/json",
        status_code=201 if login_result.is_new else 200,
    )
    set_refresh_cookie(request, response, res.settings, login_result.refresh_token)
    return response


@router.post("/refresh", response_model=LoginOutSchema, description="get a new access token")
async def refresh(res: ResourcesDep, request: Request):
    refresh_token = request.cookies.get(res.settings.cookie_name)
    if refresh_token is None:
        raise exceptions.InvalidSession()
    login_result = await res.auth.refresh(refresh_token)
    # return profile anyway, why not?
    out = LoginOutSchema(
        user_id=login_result.user.user_id,
        profile=login_result.user.profile,
        access_token=login_result.access_token,
    )
    response = Response(out.model_dump_json(), media_type="content/json")
    clear_refresh_cookie(request, response, res.settings)
    set_refresh_cookie(request, response, res.settings, login_result.refresh_token)
    return response


@router.post("/logout", description="logout of session (deletes session cookie)")
async def logout(res: ResourcesDep, request: Request, response: Response):
    await res.auth.logout(request.cookies.get(res.settings.cookie_name))
    clear_refresh_cookie(request, response, res.settings)


@router.get("/sessions")
async def list_sessions(res: ResourcesDep, user: ActiveUserDep) -> ManySessionsSchema:
    # sensitive information, extra against for stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        raise exceptions.InvalidSession()
    return ManySessionsSchema(sessions=await res.sessions.list_user_session(user.user_id))


@router.delete("/sessions")
async def terminate_all_sessions(res: ResourcesDep, user: ActiveUserDep):
    await res.sessions.terminate_all_for_user(user.user_id)


@router.delete("/sessions/{session_id}")
async def terminate_session(res: ResourcesDep, session_id: UUID4, user: ActiveUserDep):
    if not await res.sessions.terminate_for_user(user.user_id, session_id):
        raise exceptions.NotFound()
