from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from encosim_server.auth.exceptions import RESTRICTED, UNAUTHORIZED
from encosim_server.core.security import decode_access_token
from encosim_server.resources import Resources

bearer = HTTPBearer(auto_error=False)


async def get_resources(request: Request) -> Resources:
    return request.state.resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


@dataclass
class ActiveUser:
    user_id: UUID
    session_id: UUID


async def get_active_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    res: ResourcesDep,
) -> ActiveUser:
    if creds is None:
        raise UNAUTHORIZED
    try:
        claims = decode_access_token(res.settings, creds.credentials)
    except jwt.PyJWTError:
        raise UNAUTHORIZED from None
    return ActiveUser(user_id=claims.sub, session_id=claims.sid)


ActiveUserDep = Annotated[ActiveUser, Depends(get_active_user)]


async def get_active_admin(user: ActiveUserDep, res: ResourcesDep) -> ActiveUser:
    # admin access should always be secure, so database performance is not that important
    # checking user and session protects resources from stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        await res.sessions.terminate(user.session_id)
        raise UNAUTHORIZED
    potential_admin = await res.users.get(user.user_id)
    if potential_admin is None:
        await res.sessions.terminate(user.session_id)
        raise UNAUTHORIZED
    if not potential_admin.admin:
        raise RESTRICTED
    return user


ActiveAdminDep = Annotated[ActiveUser, Depends(get_active_admin)]
