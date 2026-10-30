import asyncio
import logging
import sys
import warnings
from contextlib import asynccontextmanager
from typing import TypedDict

import uvicorn
from fastapi import FastAPI
from uvicorn.logging import DefaultFormatter

from encosim_server.auth.google import FakeGoogleVerifier, RealGoogleVerifier
from encosim_server.auth.router import router as auth_router
from encosim_server.characters.router import router as char_router
from encosim_server.core import errors
from encosim_server.core.settings import Settings, get_settings
from encosim_server.database import connect_mongo, health_check_mongo, health_check_redis
from encosim_server.resources import Resources, build_resources
from encosim_server.simulation.router import router as simu_router
from encosim_server.users.router import router as user_router

# setup logger
logger = logging.getLogger("encosim_server")
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(DefaultFormatter(fmt="%(levelprefix)s %(message)s", use_colors=True))
logger.addHandler(handler)


class AppState(TypedDict):
    resources: Resources


def create_app(settings: Settings | None = None) -> FastAPI:
    # https://github.com/Kludex/fastapi-tips?tab=readme-ov-file#6-use-lifespan-state-instead-of-appstate

    settings = settings or get_settings()
    settings.warn_on_insecure_settings()

    logger.setLevel(settings.log_level)
    if settings.google_fake_provider:
        google = FakeGoogleVerifier()
    else:
        google = RealGoogleVerifier(settings.google_client_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with connect_mongo(settings) as mongo:
            yield AppState(resources=build_resources(settings, google, mongo))

    # using uvicorn's root_path causes double prefixes, which breaks docs
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The `root_path` parameter is deprecated.*")
        app = FastAPI(lifespan=lifespan, debug=True, root_path="/api")

    app.add_exception_handler(errors.ApiError, errors.handle_api_error)  # type: ignore
    for router in (auth_router, char_router, user_router, simu_router):
        app.include_router(router)
    return app


async def preflight(settings: Settings):
    await health_check_mongo(settings)
    await health_check_redis(settings)


def main():
    settings = get_settings()
    asyncio.run(preflight(settings))
    uvicorn.run(
        "encosim_server.main:create_app",
        factory=True,
        host=settings.bind,
        port=settings.port,
        workers=settings.workers,
    )


if __name__ == "__main__":
    main()
