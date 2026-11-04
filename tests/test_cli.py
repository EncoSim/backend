"""Command line parsing, the argument-less app factory and the startup checks."""

import os
import sys
from pathlib import Path

import pytest

from encosim_server import cli
from encosim_server.app import factory
from encosim_server.core.configfile import ConfigFileWriter
from encosim_server.core.settings import CONFIG_FILE_VAR, Settings
from encosim_server.startup import preflight


@pytest.fixture
def config_file(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fp = tmp_path / "settings.json"
    ConfigFileWriter(fp, settings).save_settings()
    monkeypatch.setenv(CONFIG_FILE_VAR, "restored-by-monkeypatch")  # main() selects the path
    return fp


def test_serve_options():
    args = cli.build_parser().parse_args(["serve", "--config", "config.json", "--reload"])

    assert args.command == "serve" and args.reload is True


def test_worker_passes_everything_after_the_separator_on_to_celery(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = []
    monkeypatch.setattr(cli, "worker", lambda settings, **kw: calls.append((settings, kw)))

    cli.main(
        ["worker", "--config", str(config_file), "--include", "a.b", "--include", "c", "--",
         "--pool=prefork", "--concurrency=2"],
    )  # fmt: skip

    assert len(calls) == 1
    assert calls[0][1] == {
        "include": ["a.b", "c"],
        "celery_args": ["--pool=prefork", "--concurrency=2"],
    }


def test_worker_without_celery_arguments(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(cli, "worker", lambda settings, **kw: calls.append(kw))

    cli.main(["worker", "--config", str(config_file)])

    assert calls == [{"include": [], "celery_args": []}]


def test_the_config_option_selects_settings_and_passes_locator_to_spawned_processes(
    config_file: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    seen = []
    monkeypatch.setattr(cli, "serve", lambda s, **kw: seen.append((s, kw)))

    cli.main(["serve", "--config", str(config_file), "--reload"])

    assert seen == [(settings, {"reload": True})]
    # uvicorn's reloader and workers load their settings from the environment variable
    assert os.environ[CONFIG_FILE_VAR] == str(config_file.resolve())


def test_dev_returns_the_exit_code_of_the_supervisor(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cli, "dev", lambda settings: 7)

    assert cli.main(["dev", "--config", str(config_file)]) == 7


@pytest.mark.parametrize(
    ("alias", "expected"),
    [("serve_main", "serve"), ("worker_main", "worker"), ("dev_main", "dev")],
)
def test_script_aliases_map_to_the_commands(
    alias: str, expected: str, monkeypatch: pytest.MonkeyPatch
):
    seen = []  # type:ignore
    monkeypatch.setattr(cli, "main", lambda argv=None: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["script", "--config", "x.json"])

    assert getattr(cli, alias)() == 0

    assert seen == [[expected, "--config", "x.json"]]


def test_the_factory_builds_the_app_from_the_config_file_locator(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(CONFIG_FILE_VAR, str(config_file))

    paths = set(factory().openapi()["paths"])
    assert {"/auth/login/google", "/simu", "/user/profile"} <= paths


async def test_preflight_passes_with_everything_running(settings: Settings):
    await preflight(settings)


async def test_preflight_names_the_missing_service(settings: Settings, tmp_path: Path):
    no_redis = settings.model_copy(update={"redis_conn": tmp_path / "missing.sock"})

    with pytest.raises(RuntimeError, match="Redis"):
        await preflight(no_redis, mongo=False)
