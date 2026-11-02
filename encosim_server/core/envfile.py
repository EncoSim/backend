"""Serialize a `Settings` object to an env file that loads back into an equal object.

Used by `bootstrap` (to write the `.env.*` files) and by the supervisor (to hand its settings to
the processes it starts).
"""

import io
import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic_core import to_jsonable_python

from encosim_server.core.settings import Settings, SettingsModel


def write_file(path: Path, content: str, mode: int = 0o600, exclusive: bool = False) -> None:
    """Write a text file that is never group/world readable by accident."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, mode)


class EnvFileEncoder:
    _SAFE = re.compile(r"^[A-Za-z0-9_./:@%+,\-]+$")

    def __init__(self, fp: Path, model: SettingsModel):
        self.fp = fp
        self.settings = Settings.model_validate(model, from_attributes=True)

    def save_settings(self):
        serialized = self.settings_to_env()
        self.verify_env(serialized)
        self.fp.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.fp, serialized)

    def settings_to_env(self) -> str:
        cls = type(self.settings)
        cfg = cls.model_config
        prefix = cfg.get("env_prefix", "") or ""
        case_sensitive = cfg.get("case_sensitive", False)

        lines = []
        for name, field in cls.model_fields.items():
            value = getattr(self.settings, name)
            if value is None:
                continue  # unset: let the default / absence apply

            alias = field.validation_alias
            key = alias if isinstance(alias, str) else f"{prefix}{name}"
            if not case_sensitive:
                key = key.upper()

            lines.append(f"{key}={self._quote(self._to_str(value))}")
        return "\n".join(lines) + "\n"

    def verify_env(self, env_text: str | None = None) -> None:
        """Raise if env_text doesn't load back into an equal settings object."""
        from unittest.mock import patch  # optional dependency

        cls = type(self.settings)
        env_text = env_text if env_text is not None else self.settings_to_env()

        parsed = dotenv_values(stream=io.StringIO(env_text))

        # clear=True keeps the real environment from leaking into the check
        with patch.dict(os.environ, {k: v or "" for k, v in parsed.items()}, clear=True):
            reloaded = cls(_env_file=None)  # type: ignore

        if reloaded != self.settings:
            diffs = [
                name
                for name in cls.model_fields
                if getattr(reloaded, name) != getattr(self.settings, name)
            ]
            # report field names only, never values, since they may be secrets
            raise ValueError(f"Round-trip mismatch in fields: {diffs}")

    @staticmethod
    def _unwrap(value: Any) -> Any:
        """Recursively replace secret wrappers with their real values."""
        if isinstance(value, (SecretStr, SecretBytes)):
            secret = value.get_secret_value()
            return secret.decode() if isinstance(secret, bytes) else secret
        if isinstance(value, BaseModel):
            return {k: EnvFileEncoder._unwrap(v) for k, v in value}
        if isinstance(value, dict):
            return {k: EnvFileEncoder._unwrap(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [EnvFileEncoder._unwrap(v) for v in value]
        return value

    @staticmethod
    def _to_str(value: Any) -> str:
        value = EnvFileEncoder._unwrap(value)
        if isinstance(value, bool):  # before int, bool is an int subclass
            return "true" if value else "false"
        if isinstance(value, Enum):
            return EnvFileEncoder._to_str(value.value)
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
        # dicts, lists, dates, paths, ... -> JSON (what pydantic-settings parses back)
        jsonable = to_jsonable_python(value)
        if isinstance(jsonable, str):
            return jsonable
        return json.dumps(jsonable, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _quote(s: str) -> str:
        if "${" in s:
            # python-dotenv (used by pydantic-settings) expands ${VAR}, and
            # quoting does not prevent that, so this can't round-trip safely.
            raise ValueError("Values containing '${' cannot be serialized safely")
        if EnvFileEncoder._SAFE.match(s):
            return s
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
