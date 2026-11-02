"""Logging configuration: layout of the log files, routing of noisy loggers, tagging."""

import logging
from pathlib import Path
from typing import Any

import pytest

from encosim_server.logs import (
    build_log_config,
    configure_logging,
    service_log_file,
    tagged_print,
)

TOUCHED = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "pymongo", "celery.task")


@pytest.fixture(autouse=True)
def restore_logging():
    """dictConfig is global: put everything back so other tests keep pytest's handlers."""
    saved = {
        name: (lg.handlers[:], lg.level, lg.propagate)
        for name in TOUCHED
        for lg in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def handler_types(config: dict[str, Any]) -> set[str]:
    return {h.get("class", h.get("()", "")) for h in config["handlers"].values()}


def test_every_service_gets_its_own_directory_and_file(tmp_path: Path):
    assert service_log_file(tmp_path, "celery") == tmp_path / "celery" / "celery.log"

    config = build_log_config("celery", logs_dir=tmp_path / "logs")

    assert (tmp_path / "logs" / "celery").is_dir()
    files = {h["filename"] for h in config["handlers"].values() if "filename" in h}
    assert files == {str(tmp_path / "logs" / "celery" / "celery.log")}


def test_without_logs_dir_only_the_console_is_used():
    config = build_log_config("backend", logs_dir=None)

    assert not any("FileHandler" in t for t in handler_types(config))
    assert config["root"]["handlers"] == ["console"]
    assert config["loggers"]["pymongo"] == {"level": "WARNING"}  # nowhere to redirect it to


@pytest.mark.xfail(reason="pymongo log config does not work")
def test_pymongo_is_routed_to_the_file_only(tmp_path: Path):
    config = build_log_config("backend", logs_dir=tmp_path)

    assert config["loggers"]["pymongo"]["handlers"] == ["file"]
    assert config["loggers"]["pymongo"]["propagate"] is False


def test_celery_tasks_get_a_logger_with_task_name_and_id(tmp_path: Path):
    config = build_log_config("celery", logs_dir=tmp_path, celery_tasks=True)

    task_logger = config["loggers"]["celery.task"]
    assert task_logger["propagate"] is False
    assert "%(task_name)s" in config["formatters"]["console_task"]["fmt"]


def test_every_handler_a_logger_names_exists(tmp_path: Path):
    config = build_log_config("celery", logs_dir=tmp_path, celery_tasks=True, tag="main")

    used = set(config["root"]["handlers"])
    for logger in config["loggers"].values():
        used |= set(logger.get("handlers", []))
    assert used <= set(config["handlers"])


def test_configured_logging_reaches_stdout_and_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    configure_logging("backend", logs_dir=tmp_path, level="INFO")

    logging.getLogger("encosim.test").info("hello from the app")

    assert "hello from the app" in capsys.readouterr().out
    line = service_log_file(tmp_path, "backend").read_text()
    assert "hello from the app" in line and "encosim.test" in line


@pytest.mark.xfail(reason="pymongo log config does not work")
def test_pymongo_goes_to_the_file_but_not_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    configure_logging("backend", logs_dir=tmp_path, level="DEBUG")

    logging.getLogger("pymongo.command").debug("find users")

    assert "find users" not in capsys.readouterr().out
    assert "find users" in service_log_file(tmp_path, "backend").read_text()


def test_pymongo_noise_is_dropped_without_a_file(capsys: pytest.CaptureFixture[str]):
    configure_logging("backend", logs_dir=None, level="DEBUG")

    logging.getLogger("pymongo.command").debug("find users")
    logging.getLogger("pymongo.topology").warning("server went away")

    out = capsys.readouterr().out
    assert "find users" not in out
    assert "server went away" in out


def test_the_starting_process_is_tagged_and_colored(capsys: pytest.CaptureFixture[str]):
    configure_logging("main", logs_dir=None, tag="main")

    logging.getLogger("main.test").info("starting")

    out = capsys.readouterr().out
    assert "\x1b[33m" in out and "main" in out and "| " in out and "starting" in out


def test_tagged_print_tags_every_line_of_a_traceback(capsys: pytest.CaptureFixture[str]):
    tagged_print("celery", "Traceback\n  File x\nValueError: boom")

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert all("celery" in line and "| " in line for line in lines)
