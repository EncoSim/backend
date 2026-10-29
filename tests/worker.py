"""Celery worker for the test suite, started by tests/infra.py as `python -m tests.worker`.

Uses the real `create_celery`, so config, task base class and result backend are the production
ones. The only additions are the test tasks and the process-based (prefork) pool, which is the
only pool that enforces time limits and can kill a task that never returns.
"""

import os
from pathlib import Path

from encosim_server.celery import create_celery
from encosim_server.core.settings import Settings
from tests import celery_tasks  # noqa: F401  (registers the test tasks)


def main():
    settings = Settings(_env_file=Path(os.environ["TEST_ENV_FILE"]))  # type: ignore
    app = create_celery(settings)
    app.worker_main(
        [
            "worker",
            "--pool=prefork",
            "--concurrency=2",
            "--loglevel=INFO",
            "--hostname=test@%h",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
    )


if __name__ == "__main__":
    main()
