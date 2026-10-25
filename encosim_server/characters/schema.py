from typing import Annotated
from uuid import UUID

from encosim import EntityConfig
from pydantic import AwareDatetime, BaseModel, Field, NonNegativeInt


class CharUpload(BaseModel):
    """Schema to create/modify a Character"""

    version: NonNegativeInt
    data: EntityConfig


class CharDownload(CharUpload):
    """Stored character data"""

    char_id: Annotated[UUID, Field(validation_alias="_id")]
    created_at: AwareDatetime
    modified_at: AwareDatetime
    data: EntityConfig
