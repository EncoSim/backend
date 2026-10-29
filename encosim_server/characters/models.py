from typing import Annotated
from uuid import UUID

from encosim import EntityConfig
from pydantic import AwareDatetime, ConfigDict, Field, NonNegativeInt
from pydantic.main import BaseModel


# must not inherit CharDownload, because pydantic would accept fields without validation
class CharInDB(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    owner_id: UUID
    char_id: Annotated[UUID, Field(alias="_id")]

    created_at: AwareDatetime
    modified_at: AwareDatetime
    version: NonNegativeInt
    data: EntityConfig
