from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from encosim_server.users.schemas import BanStatus, UserProfile


class UserInDB(BaseModel):
    model_config = ConfigDict(extra="allow")

    login_id: str
    last_login: AwareDatetime
    created_at: AwareDatetime
    ban_status: BanStatus
    admin: bool = False

    user_id: Annotated[UUID, Field(validation_alias="_id")]
    profile: UserProfile
