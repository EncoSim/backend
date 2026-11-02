"""Settings loading and the env file round trip (no MongoDB or Redis needed)."""

import logging
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from encosim_server.core.envfile import EnvFileEncoder
from encosim_server.core.settings import ENV_FILE_VAR, Settings

REQUIRED = {
    "google_client_id": "client-id",
    "jwt_access_key": "k" * 40,
    "mongo_conn": Path("/tmp/mongo.sock"),
    "mongo_rs": "rs0",
    "mongo_db": "db-test",
    "mongo_user": "user-test",
    "mongo_pass": "secret",
    "redis_conn": Path("/tmp/redis.sock"),
}

SECURE: dict[str, Any] = {
    "cookie_secure": True,
    "cookie_samesite": "strict",
    "log_level": "INFO",
    "auto_promote_to_admin": [],
    "google_fake_provider": False,
}


def make_settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **fields})  # type: ignore


def write_env_file(path: Path, **fields: Any) -> Path:
    EnvFileEncoder(path, make_settings(**fields)).save_settings()
    return path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("ENCOSIM_"):
            monkeypatch.delenv(key)


# --- loading -------------------------------------------------------------------------------------


def test_load_reads_the_given_file(tmp_path: Path):
    fp = write_env_file(tmp_path / "a.env", port=1234)

    assert Settings.load(fp).port == 1234


def test_load_falls_back_to_the_env_file_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_FILE_VAR, str(write_env_file(tmp_path / "a.env", port=1111)))

    assert Settings.load().port == 1111


def test_an_explicit_file_wins_over_the_env_file_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(ENV_FILE_VAR, str(write_env_file(tmp_path / "a.env", port=1111)))

    assert Settings.load(write_env_file(tmp_path / "b.env", port=2222)).port == 2222


def test_precedence_is_overrides_then_environment_then_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fp = write_env_file(tmp_path / "a.env", port=1111)
    assert Settings.load(fp).port == 1111

    monkeypatch.setenv("ENCOSIM_PORT", "2222")
    assert Settings.load(fp).port == 2222

    assert Settings.load(fp, port=3333).port == 3333


def test_a_missing_file_only_warns_when_the_environment_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(f"ENCOSIM_{name.upper()}", str(value))

    with caplog.at_level(logging.WARNING):
        settings = Settings.load(tmp_path / "missing.env")

    assert settings.mongo_db == "db-test"
    assert "not found" in caplog.text


# --- the env file round trip (the supervisor hands settings to its children this way) -------------


def test_settings_survive_the_env_file_round_trip(tmp_path: Path):
    original = make_settings(
        session_ttl=timedelta(days=3, hours=2),
        cookie_samesite="lax",
        auto_promote_to_admin=["a@example.com", "b@example.com"],
        logs_dir=Path("/tmp/log dir"),
        jwt_access_key='k"#' + "x" * 40,  # characters that need quoting
        celery_soft_timeout=timedelta(seconds=2),
    )
    fp = tmp_path / "round.env"

    EnvFileEncoder(fp, original).save_settings()  # also verifies the round trip itself

    assert Settings.load(fp) == original


def test_the_env_file_is_private(tmp_path: Path):
    fp = write_env_file(tmp_path / "private.env")

    assert stat.S_IMODE(fp.stat().st_mode) == 0o600


# --- warnings -------------------------------------------------------------------------------------


def insecure_warnings(settings: Settings, caplog: pytest.LogCaptureFixture) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert settings.warn_on_insecure_settings() is settings
    return [r.getMessage() for r in caplog.records if "INSECURE SETTINGS" in r.getMessage()]


def test_secure_settings_produce_no_warnings(caplog: pytest.LogCaptureFixture):
    assert insecure_warnings(make_settings(**SECURE), caplog) == []


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"cookie_secure": False}, "cookies"),
        ({"cookie_samesite": "lax"}, "cookies"),
        ({"log_level": "DEBUG"}, "Debug logging"),
        ({"jwt_access_key": "short"}, "JWT"),
        ({"auto_promote_to_admin": ["a@example.com"]}, "admin promotion"),
        ({"google_fake_provider": True}, "fake google"),
    ],
)
def test_each_insecure_setting_is_reported_once(
    caplog: pytest.LogCaptureFixture, override: dict[str, Any], fragment: str
):
    messages = insecure_warnings(make_settings(**{**SECURE, **override}), caplog)

    assert len(messages) == 1 and fragment in messages[0]
