from typing import Annotated
from uuid import UUID

from encosim import EntityConfig
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt
from pydantic.aliases import AliasChoices


class CharUpload(BaseModel):
    """Schema to create/modify a Character"""

    model_config = ConfigDict(extra="forbid")

    version: NonNegativeInt
    data: EntityConfig


class CharDownload(CharUpload):
    """Stored character data"""

    model_config = ConfigDict(extra="ignore")

    char_id: Annotated[UUID, Field(validation_alias=AliasChoices("_id", "char_id"))]
    created_at: AwareDatetime
    modified_at: AwareDatetime
    data: EntityConfig


class ManyCharDownload(BaseModel):
    characters: list[CharDownload]
