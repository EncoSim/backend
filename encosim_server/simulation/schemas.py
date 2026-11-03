from datetime import datetime
from typing import Annotated, Literal

from encosim import EntityConfig
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

Team = Annotated[list[EntityConfig], Field(min_length=1, max_length=50)]


class SimulationConfig(BaseModel):
    repetitions: PositiveInt
    teams: Annotated[list[Team], Field(min_length=2, max_length=10)]


# Preserve the old spelling for downstream imports during this refactor.
SimulatonConfig = SimulationConfig


class SimulationFinished(BaseModel):
    status: Literal["failed", "success"]
    summary: str


class SimulationPending(BaseModel):
    status: Literal["pending"] = "pending"
    task_id: str


SimulationResult = Annotated[SimulationFinished | SimulationPending, Field(discriminator="status")]


class SimulationList(BaseModel):
    ids: list[str]


class SimulationRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(validation_alias="_id")
    config: SimulationConfig
    status: Literal["failed", "success"]
    result: str | None = None
    summary: str
    error_type: str | None = None
    submitted_at: datetime
    finished_at: datetime
