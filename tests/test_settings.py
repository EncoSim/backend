"""Settings loading and JSON round trips (no MongoDB or Redis needed)."""

import logging
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from encosim_server.core.configfile import ConfigFileWriter
from encosim_server.core.settings import Settings

REQUIRED = {
    "google_client_id": "client-id",
    "jwt_access_key": "k" * 40,
    "identity_hash_key": "K" * 40,
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
    return Settings.model_validate({**REQUIRED, **fields})


def write_config_file(path: Path, **fields: Any) -> Path:
    ConfigFileWriter(path, make_settings(**fields)).save_settings()
    return path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("ENCOSIM_"):
            monkeypatch.delenv(key)


def test_load_reads_the_given_json_file(tmp_path: Path):
    fp = write_config_file(tmp_path / "a.json", port=1234)
    assert Settings.load(fp).port == 1234


def test_environment_variables_never_override_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fp = write_config_file(tmp_path / "a.json", port=1111)
    monkeypatch.setenv("ENCOSIM_PORT", "2222")
    monkeypatch.setenv("ENCOSIM_CONFIG_FILE", str(tmp_path / "does-not-exist.json"))
    assert Settings.load(fp).port == 1111


def test_a_missing_file_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        Settings.load(tmp_path / "missing.json")


def test_settings_survive_the_json_round_trip(tmp_path: Path):
    original = make_settings(
        session_ttl=timedelta(days=3, hours=2),
        cookie_samesite="lax",
        auto_promote_to_admin=["a@example.com", "b@example.com"],
        logs_dir=Path("/tmp/log dir"),
        jwt_access_key='k"#' + "x" * 40,
        celery_soft_timeout=timedelta(seconds=2),
    )
    fp = tmp_path / "round.json"
    ConfigFileWriter(fp, original).save_settings()
    restored = Settings.load(fp)
    assert restored == original


def test_the_json_file_is_private(tmp_path: Path):
    fp = write_config_file(tmp_path / "private.json")
    assert stat.S_IMODE(fp.stat().st_mode) == 0o600


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
