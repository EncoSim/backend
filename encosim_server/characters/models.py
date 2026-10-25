from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field

from encosim_server.characters.schema import CharDownload


class CharInDB(CharDownload):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    owner_id: UUID
    char_id: Annotated[UUID, Field(alias="_id")]
