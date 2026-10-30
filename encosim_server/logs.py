import logging
import logging.config
import sys
import threading
from pathlib import Path
from typing import Any

CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-8s [%(process)d] %(name)s: %(message)s"
TASK_PART = " [%(task_name)s %(task_id)s]"  # celery's TaskFormatter fills these in

COLORS = {"redis": "31", "celery": "32", "backend": "36", "dev": "33"}
_print_lock = threading.Lock()


def tagged_print(name: str, text: str):
    color = COLORS.get(name, "0")
    with _print_lock:
        for line in text.splitlines() or [""]:
            sys.stdout.write(f"\x1b[{color}m{name:>8}\x1b[0m | {line}\n")
        sys.stdout.flush()


class TaggedHandler(logging.Handler):
    """Console handler that prefixes every line with the colored tag (used by the supervisor)."""

    def __init__(self, tag: str = "dev"):
        super().__init__()
        self.tag = tag

    def emit(self, record: logging.LogRecord):
        try:
            tagged_print(self.tag, self.format(record))
        except Exception:
            self.handleError(record)


def build_log_config(
    service: str,
    *,
    logs_dir: Path | None,
    level: str = "INFO",
    tag: str | None = None,  # only the supervisor sets this
    celery_tasks: bool = False,
) -> dict[str, Any]:
    """log config that allows services to log together to terminal and individually to file"""
    formatters: dict[str, Any] = {
        "console": {"format": CONSOLE_FORMAT, "datefmt": "%H:%M:%S"},
        "file": {"format": FILE_FORMAT},
    }
    handlers: dict[str, Any] = {}

    def add_handlers(suffix: str = "") -> list[str]:
        console = {"formatter": "console" + suffix}
        if tag:
            console["()"] = f"{__name__}.TaggedHandler"
            console["tag"] = tag
        else:
            console.update({"class": "logging.StreamHandler", "stream": "ext://sys.stdout"})
        handlers["console" + suffix] = console
        names = ["console" + suffix]
        if logs_dir:
            handlers["file" + suffix] = {
                "class": "logging.handlers.WatchedFileHandler",
                "filename": str(logs_dir / f"{service}.log"),
                "encoding": "utf-8",
                "formatter": "file" + suffix,
            }
            names.append("file" + suffix)
        return names

    if logs_dir:
        logs_dir.mkdir(parents=True, exist_ok=True)

    loggers: dict[str, Any] = {
        # uvicorn would otherwise keep its own handlers; route everything to the root handlers
        name: {"handlers": [], "level": level, "propagate": True}
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }

    root_handlers = add_handlers()
    file_handlers = [name for name in root_handlers if name.endswith("_file")]
    if file_handlers:
        # pymongo only to the file: no propagation, or it reaches the console via the root again
        loggers["pymongo"] = {"handlers": file_handlers, "level": level, "propagate": False}
    else:
        # no logs_dir: nothing to redirect to, so only keep warnings and errors
        loggers["pymongo"] = {"level": "WARNING"}

    if celery_tasks:  # task logs get the task name and id in front of the message
        for kind, base in (("console", CONSOLE_FORMAT), ("file", FILE_FORMAT)):
            formatters[f"{kind}_task"] = {
                "()": "celery.app.log.TaskFormatter",
                "fmt": base.replace("%(name)s", "%(name)s" + TASK_PART),
                "use_color": False,
            }
        loggers["celery.task"] = {
            "handlers": add_handlers("_task"),
            "level": level,
            "propagate": False,
        }

    return {
        "version": 1,
        "disable_existing_loggers": False,  # loggers created at import time must survive
        "formatters": formatters,
        "handlers": handlers,
        "root": {"level": level, "handlers": root_handlers},
        "loggers": loggers,
    }


def configure_logging(service: str, **kwargs: Any):
    logging.config.dictConfig(build_log_config(service, **kwargs))
