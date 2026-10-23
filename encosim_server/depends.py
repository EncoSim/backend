from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from encosim_server.core.security import Roles, decode_access_token
from encosim_server.resources import Resources

bearer = HTTPBearer(auto_error=False)


async def get_resources(request: Request) -> Resources:
    return request.state.resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


@dataclass
class ActiveUser:
    user_id: UUID
    session_id: UUID
    roles: list[Roles]


async def get_active_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    res: ResourcesDep,
) -> ActiveUser:
    unauthorized = HTTPException(401, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        claims = decode_access_token(res.settings, creds.credentials)
    except jwt.PyJWTError:
        raise unauthorized from None
    return ActiveUser(user_id=claims.sub, session_id=claims.sid, roles=claims.roles)


ActiveUserDep = Annotated[ActiveUser, Depends(get_active_user)]


async def get_active_admin(user: ActiveUserDep) -> ActiveUser:
    # admin access should always be secure, so database performance is not that important
    if "admin" not in user.roles:
        raise HTTPException(403)

    return user
