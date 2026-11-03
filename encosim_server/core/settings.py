import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import EmailStr, Field, PositiveInt, SecretStr
from pydantic.main import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

ENV_FILE_VAR = "ENCOSIM_ENV_FILE"
DEFAULT_ENV_FILE = ".env"


# split settings into model and settings to exclude environment variables during auto-generation
class SettingsModel(BaseModel):
    # uvicorn
    port: PositiveInt = Field(8000, lt=2**16)
    bind: str = "0.0.0.0"
    workers: PositiveInt = 4  # ignored outside of production

    # logging
    log_level: str = "INFO"
    logs_dir: Path | None = None  # TODO: setup logging properly!

    # login providers
    # login providers / google
    google_client_id: str
    google_fake_provider: bool = False

    # authentication
    session_ttl: timedelta = timedelta(weeks=4)

    # authentication / jwt
    jwt_access_key: SecretStr
    jwt_access_ttl: timedelta = timedelta(minutes=15)
    jwt_alg: str = "HS256"

    # authentication / cookie
    cookie_name: str = "Refresh-Token"
    cookie_path: str = "/auth"
    cookie_secure: bool = True
    cookie_samesite: Literal["none", "lax", "strict"] = "strict"

    # authentication / admins
    # for testing and demos only
    auto_promote_to_admin: list[EmailStr] = Field(default_factory=list)

    # authentication / session
    max_sessions_per_user: int = 10

    # priviledges
    max_simulation_participants: PositiveInt = 15
    max_simulation_repetitions: PositiveInt = 50
    max_simulations: PositiveInt = 10
    max_finished_simulations_per_user: PositiveInt = 10
    max_characters: PositiveInt = 30

    # database
    mongo_conn: Path
    mongo_rs: str
    mongo_db: str
    mongo_user: str
    mongo_pass: SecretStr

    # celery
    redis_conn: Path
    celery_soft_timeout: timedelta = timedelta(minutes=4)
    celery_hard_timeout: timedelta = timedelta(minutes=5)

class Settings(SettingsModel, BaseSettings):
    model_config = SettingsConfigDict(frozen=True, env_prefix="ENCOSIM_", extra="ignore")

    @classmethod
    def load(cls, env_file: Path | str | None = None, **overrides: Any) -> Self:
        """The one way to get settings: explicit file, else `$ENCOSIM_ENV_FILE`, else `./.env`.

        Precedence (highest first): `overrides`, `ENCOSIM_*` environment variables, the file.
        """
        fp = Path(env_file or os.environ.get(ENV_FILE_VAR, DEFAULT_ENV_FILE))
        if not fp.is_file():
            logger.warning(f"Environment file {fp} not found")
        return cls(_env_file=fp, **overrides)  # type: ignore

    def warn_on_insecure_settings(self):
        if self.cookie_samesite != "strict" or not self.cookie_secure:
            logger.warning("INSECURE SETTINGS: Session cookies are not secure!")
        if self.log_level in ("DEBUG", "TRACE"):
            logger.warning("INSECURE SETTINGS: Debug logging may leak data!")
        if len(self.jwt_access_key.get_secret_value()) < 32:
            logger.warning("INSECURE SETTINGS: JWT are not secure!")
        if len(self.auto_promote_to_admin) > 0:
            logger.warning("INSECURE SETTINGS: Automatic admin promotion is enabled!")
        if self.google_fake_provider:
            logger.warning("INSECURE SETTINGS: Using fake google authentication!")
        return self
