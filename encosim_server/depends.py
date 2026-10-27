from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from encosim_server.core import errors
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

async def get_optional_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    res: ResourcesDep,
):
    if creds is None:
        return
    try:
        claims = decode_access_token(res.settings, creds.credentials)
    except jwt.PyJWTError:
        return
    return ActiveUser(user_id=claims.sub, session_id=claims.sid)

OptionalUserDep = Annotated[ActiveUser | None, Depends(get_optional_user)]


async def get_active_user(maybe_user: OptionalUserDep) -> ActiveUser:
    if maybe_user is None:
        raise errors.Unauthorized()
    return maybe_user


ActiveUserDep = Annotated[ActiveUser, Depends(get_active_user)]


async def get_active_admin(user: ActiveUserDep, res: ResourcesDep) -> ActiveUser:
    # admin access should always be secure, so database performance is not that important
    # checking user and session protects resources from stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        await res.sessions.terminate(user.session_id)
        raise errors.Restricted()
    potential_admin = await res.users.get(user.user_id)
    if potential_admin is None:
        await res.sessions.terminate(user.session_id)
        raise errors.Unauthorized()
    if not potential_admin.admin:
        raise errors.Restricted()
    return user


ActiveAdminDep = Annotated[ActiveUser, Depends(get_active_admin)]
