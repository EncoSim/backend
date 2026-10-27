from typing import Annotated

from encosim import EntityConfig
from pydantic import BaseModel, Field, PositiveInt

Team = Annotated[list[EntityConfig], Field(min_lenth=1, max_length=50)]


class SimulatonSchema(BaseModel):
    repetitions: PositiveInt
    teams: Annotated[list[Team], Field(min_length=2, max_length=10)]
