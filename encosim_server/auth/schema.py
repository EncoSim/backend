from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from encosim_server.users.schemas import UserProfile


class LoginSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    profile: UserProfile
    access_token: str


class SessionSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: Annotated[UUID, Field(validation_alias="_id")]
    info: str
    last_used_at: AwareDatetime
    expires_at: AwareDatetime
