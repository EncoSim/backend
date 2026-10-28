import asyncio

import uvicorn

from encosim_server.app import create_app, load_settings_from_env_file, preflight
from encosim_server.celery import create_celery

ENV_FP = ".env.staging"


def create_staging():
    return create_app(settings=load_settings_from_env_file(ENV_FP))


def backend():
    asyncio.run(preflight(load_settings_from_env_file(ENV_FP)))
    uvicorn.run(
        "encosim_server.scripts.dev:create_staging",
        factory=True,
        reload=True,
        reload_excludes=["test*"],
        reload_includes=[ENV_FP],
        host="0.0.0.0",
        port=8001,
    )


def celery():
    settings = load_settings_from_env_file(ENV_FP)
    asyncio.run(preflight(settings))
    app = create_celery(settings)
    app.worker_main(["worker", "--loglevel=INFO"])
    return app
