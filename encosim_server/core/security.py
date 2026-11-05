import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

import jwt
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ValidationError

from encosim_server.core.settings import Settings


def utcnow() -> datetime:
    """Timezone-aware 'now', truncated to ms (MongoDB's precision)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --- access tokens (JWT) ---------------------------------------------------


class AccessTokenClaims(BaseModel):
    sub: Annotated[UUID, BeforeValidator(lambda x: UUID(hex=x))]
    sid: Annotated[UUID, BeforeValidator(lambda x: UUID(hex=x))]
    typ: Literal["access"]
    iat: AwareDatetime
    exp: AwareDatetime


def create_access_token(
    settings: Settings,
    *,
    user_id: UUID,
    session_id: UUID,
) -> str:
    now = utcnow()
    return jwt.encode(
        {
            "sub": user_id.hex,
            "sid": session_id.hex,
            "typ": "access",
            "iat": now,
            "exp": now + settings.jwt_access_ttl,
        },
        settings.jwt_access_key.get_secret_value(),
        algorithm=settings.jwt_alg,
    )


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    """Raises jwt.PyJWTError on any problem."""
    claims = jwt.decode(
        token,
        settings.jwt_access_key.get_secret_value(),
        algorithms=[settings.jwt_alg],
        options={"require": ["exp", "sub", "sid"]},
    )
    try:
        return AccessTokenClaims.model_validate(claims)
    except ValidationError as e:
        raise jwt.InvalidTokenError(e.args) from None


# --- refresh tokens (opaque: "<sid>.<secret>", only the hash is stored) -----


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_refresh_token(sid: UUID) -> tuple[str, str]:
    """Returns (token for the client, hash for the database)."""
    secret = secrets.token_urlsafe(32)
    return f"{sid.hex}.{secret}", hash_secret(secret)


def split_refresh_token(token: str) -> tuple[UUID, str] | None:
    """Returns (session id, hashed secret for database)"""
    session_id, _, secret = token.partition(".")
    if secret == "":
        return
    try:
        session_id = UUID(hex=session_id)
    except ValueError:
        return
    return (session_id, hash_secret(secret))


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
