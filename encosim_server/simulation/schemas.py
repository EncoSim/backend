from typing import Annotated, Literal

from encosim import EntityConfig
from pydantic import BaseModel, Discriminator, Field, PositiveInt

Team = Annotated[list[EntityConfig], Field(min_length=1, max_length=50)]


class SimulatonConfig(BaseModel):
    repetitions: PositiveInt
    teams: Annotated[list[Team], Field(min_length=2, max_length=10)]


class SimulationFinished(BaseModel):
    status: Literal["failed", "success"]
    summary: str


class SimulationPending(BaseModel):
    status: Literal["pending"] = "pending"
    task_id: str


SimulationResult = Annotated[SimulationFinished | SimulationPending, Discriminator("status")]


class SimulationList(BaseModel):
    ids: list[str]
