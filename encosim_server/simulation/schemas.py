from datetime import datetime
from typing import Annotated, Literal, Self

from encosim import EntityConfig
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt
from pydantic.functional_validators import model_validator

Team = Annotated[list[NonNegativeInt], Field(min_length=1, max_length=10)]


class SimulationConfig(BaseModel):
    """Simulation input. Team members are zero-based indices into `participants`."""

    repetitions: Annotated[PositiveInt, Field(description="Number of simulation repetitions.")]
    participants: Annotated[
        list[EntityConfig],
        Field(min_length=1, description="Entities referenced by the teams array."),
    ]
    teams: Annotated[
        list[Team],
        Field(
            min_length=2,
            max_length=10,
            description="Teams as lists of zero-based participant indices",
        ),
    ]

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


class _SimulationBase(BaseModel):
    """Fields stored for every simulation from the moment it is submitted."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(validation_alias="_id")
    config: SimulationConfig
    submitted_at: datetime


class SimulationPendingRecord(_SimulationBase):
    """A simulation that is queued or running."""

    status: Literal["pending"] = "pending"


class SimulationRecord(_SimulationBase):
    """A finished simulation as kept in the user's history."""

    status: Literal["failed", "success"]
    summary: str
    error_type: str | None = None
    finished_at: datetime


SimulationEntry = Annotated[
    SimulationPendingRecord | SimulationRecord, Field(discriminator="status")
]
