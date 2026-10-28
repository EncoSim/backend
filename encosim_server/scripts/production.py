import asyncio

import uvicorn

from encosim_server.app import preflight
from encosim_server.celery import create_celery
from encosim_server.core.settings import Settings


def backend():
    asyncio.run(preflight())
    uvicorn.run(
        "encosim_server.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=4,
    )


def celery():
    settings = Settings()  # type: ignore
    asyncio.run(preflight(settings))
    app = create_celery(settings)
    app.worker_main(["worker"])
    return app
