"""Write validated settings as private JSON configuration files."""

import json
import os
from pathlib import Path

from pydantic import SecretStr

from encosim_server.core.settings import Settings


def write_file(path: Path, content: str, mode: int = 0o600, exclusive: bool = False) -> None:
    """Write a text file that is never group/world readable by accident."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, mode)


class ConfigFileWriter:
    """Serialize settings to JSON and verify that they round-trip without losing secrets."""

    def __init__(self, fp: Path, settings: Settings):
        self.fp = fp
        self.settings = settings

    def save_settings(self) -> None:
        data = self.settings.model_dump(mode="json")
        # Pydantic intentionally masks SecretStr in JSON output. Configuration files must
        # contain the actual credentials so child processes can authenticate to their services.
        for name in type(self.settings).model_fields:
            value = getattr(self.settings, name)
            if isinstance(value, SecretStr):
                data[name] = value.get_secret_value()

        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        reloaded = Settings.model_validate_json(content)
        assert reloaded == self.settings
        if reloaded != self.settings:
            diffs = [
                name
                for name in type(self.settings).model_fields
                if getattr(reloaded, name) != getattr(self.settings, name)
            ]
            raise ValueError(f"JSON round-trip mismatch in fields: {diffs}")

        self.fp.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.fp, content)
