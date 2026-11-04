"""The command line: `encosim serve | worker | dev`.

The older script names (`main`, `celery`, `dev`) are aliases for the three commands.
Settings are loaded from an explicitly selected JSON file (`--config`).
"""

import argparse
import importlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from celery.signals import setup_logging

from encosim_server.celery import create_celery
from encosim_server.core.settings import CONFIG_FILE_VAR, Settings
from encosim_server.logs import build_log_config, configure_logging
from encosim_server.startup import startup
from encosim_server.supervisor import PACKAGE_DIR, Supervisor, stack_specs


def serve(settings: Settings, *, reload: bool = False):
    """Run the backend with uvicorn."""
    startup(settings, "backend")
    uvicorn.run(
        "encosim_server.app:factory",
        factory=True,
        host=settings.bind,
        port=settings.port,
        workers=None if reload else settings.workers,
        reload=reload,
        reload_dirs=[str(PACKAGE_DIR)] if reload else None,
        log_config=build_log_config(
            "backend", logs_dir=settings.logs_dir, level=settings.log_level
        ),
    )


def worker(settings: Settings, *, include: Sequence[str] = (), celery_args: Sequence[str] = ()):
    """Run a celery worker. `include` names extra modules with tasks (the tests use this)."""
    startup(settings, "celery", celery_tasks=True)
    for module in include:
        importlib.import_module(module)
    app = create_celery(settings)

    @setup_logging.connect(weak=False, dispatch_uid="encosim-logging")
    def _keep_our_logging(**_):  # with a receiver connected, celery leaves logging alone
        configure_logging(
            "celery", logs_dir=settings.logs_dir, level=settings.log_level, celery_tasks=True
        )

    app.worker_main(["worker", f"--loglevel={settings.log_level}", *celery_args])


def dev(settings: Settings) -> int:
    """Run redis, celery and the backend together, restarting them on crashes and changes."""
    startup(settings, "main", tag="main", mongo=True, redis=False)
    with Supervisor(settings, stack_specs(settings, reload=True)) as supervisor:
        return supervisor.run_forever()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, required=True, help="JSON configuration file")

    parser = argparse.ArgumentParser(prog="encosim", description="EncoSim backend")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", parents=[common], help="run the backend")
    serve_parser.add_argument("--reload", action="store_true", help="restart on code changes")

    worker_parser = commands.add_parser("worker", parents=[common], help="run a celery worker")
    worker_parser.add_argument(
        "--include", action="append", default=[], metavar="MODULE", help="extra task module"
    )
    worker_parser.add_argument(
        "celery_args", nargs=argparse.REMAINDER, help="after `--`: passed on to celery"
    )

    commands.add_parser("dev", parents=[common], help="redis, celery and backend with reload")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_file = args.config
    config_file = config_file.expanduser().resolve()
    # Uvicorn reloaders/workers import the app factory in fresh processes. This variable carries
    # only the selected file path; Settings never reads setting values from the environment.
    os.environ[CONFIG_FILE_VAR] = str(config_file)
    settings = Settings.load(config_file)

    match args.command:
        case "serve":
            serve(settings, reload=args.reload)
        case "worker":
            celery_args = args.celery_args
            if celery_args[:1] == ["--"]:
                celery_args = celery_args[1:]
            worker(settings, include=args.include, celery_args=celery_args)
        case "dev":
            return dev(settings)
    return 0


# script aliases: `uv run main`, `uv run celery`, `uv run dev`
def serve_main() -> int:
    return main(["serve", *sys.argv[1:]])


def worker_main() -> int:
    return main(["worker", *sys.argv[1:]])


def dev_main() -> int:
    return main(["dev", *sys.argv[1:]])
