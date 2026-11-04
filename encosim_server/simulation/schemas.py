from datetime import datetime
from typing import Annotated, Literal, Self

from encosim import EntityConfig
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt
from pydantic.functional_validators import model_validator

Team = Annotated[list[NonNegativeInt], Field(min_length=1, max_length=10)]


class SimulationConfig(BaseModel):
    repetitions: PositiveInt
    participants: Annotated[list[EntityConfig], Field(min_length=1)]
    teams: Annotated[list[Team], Field(min_length=2, max_length=10)]

    @model_validator(mode="after")
    def check_teams(self) -> Self:
        for i, team in enumerate(self.teams):
            for j, member in enumerate(team):
                if member >= len(self.participants):
                    raise ValueError(f"Party member {j} in team {i} is not listed in participants")
        return self

    def as_pure_list(self) -> list[list[EntityConfig]]:
        out = []
        for team in self.teams:
            t = []
            for member in team:
                t.append(self.participants[member])
            out.append(t)
        return out


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
