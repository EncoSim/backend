import sys
import warnings
from contextlib import asynccontextmanager
from typing import TypedDict

from fastapi import FastAPI

from encosim_server.auth.google import FakeGoogleVerifier, RealGoogleVerifier
from encosim_server.auth.router import router as auth_router
from encosim_server.characters.router import router as char_router
from encosim_server.core import errors
from encosim_server.core.settings import Settings
from encosim_server.database import connect_mongo
from encosim_server.resources import Resources, build_resources
from encosim_server.simulation.router import router as simu_router
from encosim_server.users.router import router as user_router


class AppState(TypedDict):
    resources: Resources


def create_app(settings: Settings) -> FastAPI:
    """Build the application. No side effects: logging and checks belong to `startup`."""
    # https://github.com/Kludex/fastapi-tips?tab=readme-ov-file#6-use-lifespan-state-instead-of-appstate

    if settings.google_fake_provider:
        google = FakeGoogleVerifier()
    else:
        google = RealGoogleVerifier(settings.google_client_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with connect_mongo(settings) as mongo:
            resources = build_resources(settings, google, mongo)
            try:
                yield AppState(resources=resources)
            finally:
                await resources.celery.simulation_limiter.close()
                resources.celery.close_backend()

    # using uvicorn's root_path causes double prefixes, which breaks docs
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The `root_path` parameter is deprecated.*")
        app = FastAPI(lifespan=lifespan, debug=True, root_path="/api")

    app.add_exception_handler(errors.ApiError, errors.handle_api_error)  # type: ignore
    for router in (auth_router, char_router, user_router, simu_router):
        app.include_router(router)
    return app


def factory() -> FastAPI:
    """Argument-less factory for uvicorn: reloader and workers import the app in fresh processes,
    where the settings come from `$ENCOSIM_ENV_FILE` (see `Settings.load`)."""
    return create_app(Settings.load())


def main():
    """Kept so that `python -m encosim_server.app` (used by the supervisord config) still works."""
    from encosim_server.cli import main as cli_main

    raise SystemExit(cli_main(["serve", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
