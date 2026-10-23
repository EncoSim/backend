import logging
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import EmailStr, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, env_prefix="ENCOSIM_", extra="ignore")

    # logging
    log_level: str = "INFO"

    # login providers
    # login providers / google
    google_client_id: str

    # authentication
    session_ttl: timedelta

    # authentication / jwt
    jwt_access_key: SecretStr
    jwt_access_ttl: timedelta
    jwt_alg: str = "HS256"

    # authentication / cookie
    cookie_name: str
    cookie_path: str
    cookie_secure: bool
    cookie_samesite: Literal["none", "lax", "strict"]

    # authentication / admins
    # for testing and demos only
    auto_promote_to_admin: list[EmailStr]

    # authentication / session
    max_sessions_per_user: int

    # priviledges
    max_simulation_participants: PositiveInt = 15
    max_simulation_repetitions: PositiveInt = 50
    max_characters: PositiveInt = 30
    max_simulation_participants_anon: PositiveInt = 10
    max_simulation_repetitions_anon: PositiveInt = 10

    # database
    mongo_conn: Path
    mongo_rs: str = "rs0"
    mongo_db: str
    mongo_user: str
    mongo_pass: SecretStr


def warn_on_insecure_settings(settings: Settings):
    if settings.cookie_samesite != "strict" or not settings.cookie_secure:
        logger.warning("Session cookies are not secure!")
    if settings.log_level in ("DEBUG", "TRACE"):
        logger.warning("Debug logging may leak data!")
    if len(settings.jwt_access_key.get_secret_value()) < 32:
        logger.warning("JWT are not secure!")
    if len(settings.auto_promote_to_admin) > 0:
        logger.warning("Automatic admin promotion is enabled!")
    return settings
