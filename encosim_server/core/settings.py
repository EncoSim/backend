import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PositiveInt, SecretStr

logger = logging.getLogger(__name__)

# This environment variable carries only the path to a JSON file into freshly spawned processes.
# It is required to pass the config file to processes spawned through uvicorn.
CONFIG_FILE_VAR = "ENCOSIM_CONFIG_FILE"


class Settings(BaseModel):
    """Validated application configuration loaded exclusively from an explicit JSON file."""

    model_config = ConfigDict(frozen=True)

    # uvicorn, fastapi
    port: PositiveInt = Field(8000, lt=2**16)
    bind: str = "0.0.0.0"
    workers: PositiveInt = 4
    debug: bool = False
    root_path: str = "/api"
    cors_origins: tuple[str, ...] = ()

    # logging
    log_level: str = "INFO"
    logs_dir: Path | None = None

    # login providers
    google_client_id: str
    google_fake_provider: bool = False

    # authentication
    session_ttl: timedelta = timedelta(weeks=4)

    # authentication / jwt
    jwt_access_key: SecretStr
    jwt_access_ttl: timedelta = timedelta(minutes=15)
    jwt_alg: str = "HS256"

    # authentication / cookie
    cookie_secure: bool = True
    cookie_samesite: Literal["none", "lax", "strict"] = "strict"

    # authentication / admins
    # for testing and demos only
    auto_promote_to_admin: tuple[EmailStr, ...] = ()

    # authentication / session
    max_sessions_per_user: int = 10

    # authentication / hash
    identity_hash_key: SecretStr

    # privileges
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

    # celery / redis
    redis_conn: Path
    celery_soft_timeout: timedelta = timedelta(minutes=4)
    celery_hard_timeout: timedelta = timedelta(minutes=5)

    @classmethod
    def load(cls, config_file: Path | str) -> Self:
        fp = Path(config_file).expanduser().resolve()
        if not fp.is_file():
            raise FileNotFoundError(f"Configuration file {fp} does not exist")
        return cls.model_validate_json(fp.read_text(encoding="utf-8"))

    def warn_on_insecure_settings(self):
        if self.cookie_samesite != "strict" or not self.cookie_secure:
            logger.warning("INSECURE SETTINGS: Session cookies are not secure!")
        if self.log_level in ("DEBUG", "TRACE"):
            logger.warning("INSECURE SETTINGS: Debug logging may leak data!")
        if len(self.jwt_access_key.get_secret_value()) < 32:
            logger.warning("INSECURE SETTINGS: JWT are not secure!")
        if self.auto_promote_to_admin != ():
            logger.warning("INSECURE SETTINGS: Automatic admin promotion is enabled!")
        if self.cors_origins != ():
            logger.warning("INSECURE SETTINGS: CORS is enabled!")
        if self.google_fake_provider:
            logger.warning("INSECURE SETTINGS: Using fake google authentication!")
        return self


def config_file_from_environment() -> Path:
    """Read the config-file *path* passed to spawned processes, never setting values."""
    value = os.environ.get(CONFIG_FILE_VAR)
    if not value:
        raise RuntimeError(
            f"No configuration file selected. Pass --config or set {CONFIG_FILE_VAR} "
            "to the path of a JSON configuration file."
        )
    return Path(value)
