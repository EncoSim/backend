import asyncio
import signal
import sys
import threading
import time

import uvicorn

from encosim_server import app
from encosim_server.app import preflight
from encosim_server.core.settings import get_settings
from encosim_server.logs import build_log_config, configure_logging
from encosim_server.supervisor import Service, supervisor


def main() -> int:
    """launch redis, celery and fastapi in development mode"""
    settings = get_settings()
    configure_logging("dev", logs_dir=settings.logs_dir, level=settings.log_level, tag="dev")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    with supervisor(settings) as services:
        asyncio.run(preflight(settings))
        services.append(
            Service(
                "backend",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "encosim_server.app:create_app",
                    "--factory",
                    "--reload",
                    "--port",
                    str(settings.port),
                    "--host",
                    settings.bind,
                ],
                # logs_dir=settings.logs_dir,
            )
        )
        while not stop.is_set() and all(s.alive() for s in services):
            stop.wait(1)

    died = [
        service
        for service in services
        if service.proc.returncode not in (0, None, -signal.SIGTERM, -signal.SIGKILL)
    ]
    return 0 if stop.is_set() or not died else died[0].proc.returncode
