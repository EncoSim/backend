"""What every process does when it starts, written once."""

import asyncio

from encosim_server.core.settings import Settings
from encosim_server.database import health_check_mongo, health_check_redis
from encosim_server.logs import configure_logging


async def preflight(settings: Settings, *, mongo: bool = True, redis: bool = True):
    """Fail early and readably when a service this process needs is not there."""
    if mongo:
        await health_check_mongo(settings)
    if redis:
        await health_check_redis(settings)


def startup(
    settings: Settings,
    service: str,
    *,
    tag: str | None = None,
    celery_tasks: bool = False,
    mongo: bool = True,
    redis: bool = True,
):
    """Configure logging, warn about risky settings and check the services this process needs.

    `service` names the log directory and the tag, `tag` additionally prefixes console lines
    (only the process that merges the output of the others does that).
    """
    configure_logging(
        service,
        logs_dir=settings.logs_dir,
        level=settings.log_level,
        tag=tag,
        celery_tasks=celery_tasks,
    )
    settings.warn_on_insecure_settings()
    asyncio.run(preflight(settings, mongo=mongo, redis=redis))
