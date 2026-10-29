
from fastapi import APIRouter, Request, Response
from pydantic import UUID4

from encosim_server.auth.schema import (
    GoogleLoginInSchema,
    LoginOutSchema,
    ManySessionsSchema,
)
from encosim_server.core import errors
from encosim_server.core.security import clear_refresh_cookie, set_refresh_cookie
from encosim_server.depends import ActiveUserDep, ResourcesDep

router = APIRouter(prefix="/auth", tags=["auth"])



@router.post(
    "/login/google",
    response_model=LoginOutSchema,
    responses={201: {"model": LoginOutSchema}, **errors.to_responses(errors.InvalidGoogleToken)},
    description="login with google (sets session cookie)",
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
        media_type="content/json",
        status_code=201 if login_result.is_new else 200,
    )
    set_refresh_cookie(request, response, res.settings, login_result.refresh_token)
    return response

@router.post(
    "/refresh",
    response_model=LoginOutSchema,
    responses=errors.to_responses(errors.InvalidSession),
    description="get a new access token",
)
async def refresh(res: ResourcesDep, request: Request):
    refresh_token = request.cookies.get(res.settings.cookie_name)
    if refresh_token is None:
        raise errors.InvalidSession()
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


COMMON = (errors.InvalidSession, errors.Unauthorized)


@router.get("/sessions", responses=errors.to_responses(*COMMON))
async def list_sessions(res: ResourcesDep, user: ActiveUserDep) -> ManySessionsSchema:
    # sensitive information, check again for stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        raise errors.InvalidSession()
    return ManySessionsSchema(sessions=await res.sessions.list_user_sessions(user.user_id))


@router.delete("/sessions", responses=errors.to_responses(*COMMON))
async def terminate_all_sessions(res: ResourcesDep, user: ActiveUserDep):
    await res.sessions.terminate_all_for_user(user.user_id)


@router.delete("/sessions/{session_id}", responses=errors.to_responses(*COMMON, errors.NotFound))
async def terminate_session(res: ResourcesDep, session_id: UUID4, user: ActiveUserDep):
    if not await res.sessions.terminate_for_user(user.user_id, session_id):
        raise errors.NotFound()
