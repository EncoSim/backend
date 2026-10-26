from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_typed_errors import Raises

from encosim_server.core import exceptions
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
) -> Annotated[ActiveUser, Raises[exceptions.Unauthorized]]:
    if creds is None:
        raise exceptions.Unauthorized()
    try:
        claims = decode_access_token(res.settings, creds.credentials)
    except jwt.PyJWTError:
        raise exceptions.Unauthorized() from None
    return ActiveUser(user_id=claims.sub, session_id=claims.sid)


ActiveUserDep = Annotated[ActiveUser, Depends(get_active_user)]


async def get_active_admin(
    user: ActiveUserDep, res: ResourcesDep
) -> Annotated[ActiveUser, Raises[exceptions.Unauthorized, exceptions.Restricted]]:
    # admin access should always be secure, so database performance is not that important
    # checking user and session protects resources from stale access tokens
    if not await res.sessions.is_valid(user.user_id, user.session_id):
        await res.sessions.terminate(user.session_id)
        raise exceptions.Restricted()
    potential_admin = await res.users.get(user.user_id)
    if potential_admin is None:
        await res.sessions.terminate(user.session_id)
        raise exceptions.Unauthorized()
    if not potential_admin.admin:
        raise exceptions.Restricted()
    return user


ActiveAdminDep = Annotated[ActiveUser, Depends(get_active_admin)]
