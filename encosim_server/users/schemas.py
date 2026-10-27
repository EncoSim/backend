from typing import Annotated

from pydantic import (
    AnyUrl,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    NonNegativeInt,
)


class BanStatus(BaseModel):
    banned: bool
    reason: str | None = None
    number: NonNegativeInt = 0
    banned_at: AwareDatetime | None = None


def string_to_bool(s) -> bool:
    if not isinstance(s, str):
        raise TypeError("Expected a string")
    return s.lower() == "true"


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None
    email: Annotated[EmailStr, BeforeValidator(str.lower)] | None
    email_verified: bool = False
    picture: AnyUrl | None
