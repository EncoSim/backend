from fastapi import APIRouter, Request, Response
from pydantic import UUID4

from encosim_server.auth.cookie import (
    REFRESH_COOKIE_HEADER,
    RefreshToken,
    clear_refresh_cookie,
    set_refresh_cookie,
)
from encosim_server.auth.manager import AuthManager
from encosim_server.auth.schema import (
    GoogleLoginInSchema,
    LoginOutSchema,
    ManySessionsSchema,
)
from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login/google",
    responses={
        200: {
            "model": LoginOutSchema,
            "headers": REFRESH_COOKIE_HEADER,
            "description": "Logged in to existing account",
        },
        201: {
            "model": LoginOutSchema,
            "headers": REFRESH_COOKIE_HEADER,
            "description": "Created new account and logged in",
        },
        **errors.to_responses_from(AuthManager.login_google),
    },
    description="login with google",
)
async def login_google(res: ResourcesDep, token: GoogleLoginInSchema, request: Request) -> Response:
    login_result = await res.auth.login_google(token.id_token, request.headers.get("user-agent"))
    out = LoginOutSchema(
        user_id=login_result.user.user_id,
        profile=login_result.user.profile,
        access_token=login_result.access_token,
    )
    response = Response(
        out.model_dump_json(),
        media_type="application/json",
        status_code=201 if login_result.is_new else 200,
    )
    set_refresh_cookie(request, response, res.settings, login_result.refresh_token)
    return response


@router.post(
    "/refresh",
    response_model=LoginOutSchema,
    responses={
        200: {"headers": REFRESH_COOKIE_HEADER},
        **errors.to_responses_from(AuthManager.refresh, extra=[errors.InvalidSession]),
    },
    description="Cycle refresh token (cookie) and get a new access token",
)
async def refresh(
    res: ResourcesDep,
    request: Request,
    refresh_token: RefreshToken = None,
):
    if refresh_token is None:
        raise errors.InvalidSession()
    login_result = await res.auth.refresh(refresh_token)
    # return profile anyway, why not?
    out = LoginOutSchema(
        user_id=login_result.user.user_id,
        profile=login_result.user.profile,
        access_token=login_result.access_token,
    )
    response = Response(out.model_dump_json(), media_type="application/json")
    set_refresh_cookie(request, response, res.settings, login_result.refresh_token)
    return response


@router.post("/logout", status_code=204, description="logout of session")
async def logout(
    res: ResourcesDep,
    request: Request,
    response: Response,
    refresh_token: RefreshToken = None,
):
    await res.auth.logout(refresh_token)
    clear_refresh_cookie(request, response)


COMMON = (errors.InvalidSession, errors.Unauthorized)


@router.get("/sessions", responses=errors.to_responses(*COMMON))
async def list_sessions(res: ResourcesDep, user: ActiveUserDep) -> ManySessionsSchema:
    # sensitive information, check again for stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        raise errors.InvalidSession()
    return ManySessionsSchema(sessions=await res.sessions.list_user_sessions(user.user_id))


@router.delete(
    "/sessions",
    status_code=204,
    responses=errors.to_responses(*COMMON),
    description="Terminates all sessions, including the current one. User is logged out.",
)
async def terminate_all_sessions(
    res: ResourcesDep,
    user: ActiveUserDep,
    request: Request,
    response: Response,
    refresh_token: RefreshToken = None,
):
    await res.sessions.terminate_all_for_user(user.user_id)
    await res.auth.logout(refresh_token)
    clear_refresh_cookie(request, response)


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    responses=errors.to_responses(*COMMON, errors.NotFound),
)
async def terminate_session(res: ResourcesDep, session_id: UUID4, user: ActiveUserDep):
    if not await res.sessions.terminate_for_user(user.user_id, session_id):
        raise errors.NotFound()
