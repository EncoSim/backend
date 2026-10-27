import asyncio
import logging
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict

import uvicorn
from fastapi import FastAPI
from uvicorn.logging import DefaultFormatter

from encosim_server.auth.google import GoogleVerifier, RealGoogleVerifier
from encosim_server.auth.router import router as auth_router
from encosim_server.characters.router import router as char_router
from encosim_server.core import errors
from encosim_server.core.settings import Settings, warn_on_insecure_settings
from encosim_server.database import connect_mongo, health_check_mongo
from encosim_server.resources import Resources, build_resources
from encosim_server.users.router import router as user_router

# setup logger
logger = logging.getLogger("encosim_server")
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(DefaultFormatter(fmt="%(levelprefix)s %(message)s", use_colors=True))
logger.addHandler(handler)


class AppState(TypedDict):
    resources: Resources


def create_app(settings: Settings | None = None, google: GoogleVerifier | None = None) -> FastAPI:
    # https://github.com/Kludex/fastapi-tips?tab=readme-ov-file#6-use-lifespan-state-instead-of-appstate

    if settings is None:
        settings = Settings()  # type: ignore
    warn_on_insecure_settings(settings)
    if google is None:
        google = RealGoogleVerifier(settings.google_client_id)
    if not isinstance(google, RealGoogleVerifier):
        logger.warning("Using fake google authentication!")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.setLevel(settings.log_level)

        async with connect_mongo(settings) as mongo:
            yield AppState(resources=build_resources(settings, google, mongo))

    # using uvicorn's root_path causes double prefixes, which breaks docs
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The `root_path` parameter is deprecated.*")
        app = FastAPI(lifespan=lifespan, debug=True, root_path="/api")
    app.add_exception_handler(errors.ApiError, errors.handle_api_error)
    for router in (auth_router, char_router, user_router):
        app.include_router(router)
    return app


def load_settings_from_env_file(env_fp: str | Path) -> Settings:
    env_fp = Path(env_fp)
    if not env_fp.is_file():
        raise FileNotFoundError(f"Could not find environment file {env_fp}")
    settings = Settings(_env_file=env_fp)  # type: ignore - linter cannot know runtime variables
    return settings


async def preflight(settings: Settings | None = None):
    if settings is None:
        settings = Settings()  # type:ignore
    await health_check_mongo(settings)


def main():
    asyncio.run(preflight())
    uvicorn.run(
        "encosim_server.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=4,
    )


if __name__ == "__main__":
    main()
