"""Bootstrap MongoDB with one replica set (to enable transactions) and credentials

Will configure a root admin and 4 databases for
- development/demo on local machine (local)
- automated testing (test)
- development/demo testing on a host machine (staging)
- release on a host machine (production)

as well as environment and supervisord files for the different scenarios

File Structure
    base
     ├─ logs
     │   └─ ...
     ├─ mongo
     │   ├─ data    // actual MongoDB database
     │   │   └─ ...
     │   ├─ keyfile
     │   ├─ mongodb-<PORT>.sock
     │   └─ mongod.conf
     ├─ redis
     │   ├─ production.conf
     │   └─ ...
     ├─ .env.production
     ├─ .env.staging
     ├─ .env.local
     ├─ .env.test
     ├─ encosim.ini
     └─ README.md

NOTE:
    encosim-test.ini is also generated, but makes no sense as there is no
    server to run with supervisord, only a pytest test suit
"""

import argparse
import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import dotenv_values
from pydantic import BaseModel, EmailStr, SecretBytes, SecretStr
from pydantic_core import to_jsonable_python
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from encosim_server import PROJECT_ROOT
from encosim_server.core.settings import Settings, SettingsModel


def main():
    parser = argparse.ArgumentParser(
        "bootstrap",
        description="Perform initial setup of MongoDB",
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.getcwd()) / "database",
        help="Directory to install databases in. Default is './database'",
    )
    parser.add_argument(
        "--google",
        required=True,
        dest="google_client_id",
        help="Google Client Id. Can also use a dummy value for local environments.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=27017,
        dest="mongo_port",
        help="MongoDB always runs a TCP listener. Change in case default port (27017) is not free.",
    )
    parser.add_argument(
        "--rs",
        default="rs0",
        dest="replica_set",
        help="MongoDB replica set. Default is 'rs0'",
    )
    args = parser.parse_args()

    if args.base.exists() and not ask_if_user_wants_to_continue(
        "Base directory is not empty. This can lead to data loss!"
    ):
        return
    writer = ConfigWriter(args.base, mongo_port=args.mongo_port, replica_set=args.replica_set)
    writer.add_setup(
        name="production",
        bind="0.0.0.0",
        port=8000,
        google_client_id=args.google_client_id,
        log_level="INFO",
    )
    writer.add_setup(
        name="staging",
        bind="0.0.0.0",
        port=8001,
        google_client_id=args.google_client_id,
        log_level="DEBUG",
    )
    writer.add_setup(
        name="dev",
        bind="localhost",
        port=8000,
        log_level="DEBUG",
        google_client_id=args.google_client_id,
        fake_google_provider=True,
        admin_mails=["admin@example.com"],
        cookies_samesite="lax",
        cookies_secure=False,
    )
    writer.add_setup(
        name="test",
        bind="localhost",
        port=8001,
        google_client_id=args.google_client_id,
        log_level="DEBUG",
        cookies_secure=False,
    )
    writer.bootstrap_and_write()
    return


class ConfigWriter:
    def __init__(self, base: Path, mongo_port: int, replica_set: str):
        self.base_dir = base.resolve().absolute()
        self.logs_dir = self.base_dir / "logs"
        self.redis_dir = self.base_dir / "redis"

        self.mongo_writer = MongoBootstrapper(
            self.base_dir / "mongo",
            mongo_port,
            replica_set,
        )
        self.settings: list[EnvFileEncoder] = []

    def add_setup(
        self,
        name: str,
        bind: str,
        port: int,
        google_client_id: str,
        admin_mails: list[EmailStr] | None = None,
        **kwargs: Any,
    ):
        """The first setup is treated as the production configuration!"""
        model = SettingsModel(
            bind=bind,
            port=port,
            google_client_id=google_client_id,
            jwt_access_key=generate_password(256),
            auto_promote_to_admin=admin_mails or [],
            mongo_conn=self.mongo_writer.sock_path,
            mongo_rs=self.mongo_writer.replica_set,
            mongo_db=f"encosim-{name}-db",
            mongo_user=f"encosim-{name}-user",
            mongo_pass=generate_password(),
            redis_conn=self.redis_dir / f"redis-{name}.sock",
            **kwargs,
        )
        self.settings.append(EnvFileEncoder(self.base_dir / f".env.{name}", model))

    def bootstrap_and_write(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.redis_dir.mkdir(exist_ok=True)

        print("Bootstrapping MongoDB...")
        users = [
            MongoUser(
                db=e.settings.mongo_db,
                username=e.settings.mongo_user,
                password=e.settings.mongo_pass,
            )
            for e in self.settings
        ]
        self.mongo_writer.bootstrap(users)
        print("Done")
        print("Writing config files", end="")
        self.mongo_writer.save_config()

        def dot(end: str = ""):
            print(".", end=end, flush=True)

        for e in self.settings:
            dot()
            e.save_settings()
        dot()
        self.save_redis_config()
        dot()
        self.save_supervisord_config()
        dot(end="\n")
        self.save_readme()
        print("Done", flush=True)

    @property
    def redis_main_conf(self) -> Path:
        return self.redis_dir / "redis.conf"

    def save_redis_config(self):
        s = f"""\
        daemonize no
        port 0
        unixsocket {self.settings[0].settings.redis_conn}
        unixsocketperm 700
        dir {self.redis_dir}
        """
        write_file(self.redis_main_conf, textwrap.dedent(s))

    def save_supervisord_config(self):
        common = (
            f"environment=ENCOSIM_ENV_FILE={self.settings[0].fp}"
            f"autostart=no\n"
            f"autorestart=yes\n"
            f"startsecs=10\n"
            f"stdout_logfile={self.logs_dir}/%(program_name)s-%(process_num)s.stdout.log\n"
            f"stderr_logfile={self.logs_dir}/%(program_name)s-%(process_num)s.stderr.log\n"
        )
        write_file(
            self.base_dir / "encosim.ini",
            f"[program:mongo]\n"
            f"command=mongod --config {self.mongo_writer.conf_path}\n"
            f"{common}\n"
            f"[program:encosim-redis]\n"
            f"command=redis-server {self.redis_main_conf}\n"
            f"{common}\n"
            f"[program:encosim-celery]\n"
            f"directory={PROJECT_ROOT}\n"
            f"command=uv run celery\n"
            f"{common}\n"
            f"[program:encosim-backend]\n"
            f"directory={PROJECT_ROOT}\n"
            f"command={sys.executable} -m encosim_server.app\n"
            f"{common}\n",
        )

    def save_readme(self):
        with (self.base_dir / "README.md").open("w") as f:
            f.write(f"Automatic setup created with [`bootstrap.py`]({__file__})")
        return


@dataclass
class MongoAdmin:
    """MongoDB admin"""

    username: str
    password: SecretStr


@dataclass
class MongoUser:
    """MongoDB user with permission for one database"""

    db: str
    username: str
    password: SecretStr


class MongoBootstrapper:
    def __init__(self, mongo_dir: Path, port: int, replica_set: str):
        self.port = port
        self.replica_set = replica_set
        self.dir = mongo_dir
        self.data_dir = mongo_dir / "data"
        self.conf_path = mongo_dir / "mongod.conf"
        self.sock_path = mongo_dir / f"mongodb-{port}.sock"
        check_sock_len(self.sock_path)
        self.keyfile_path = mongo_dir / "keyfile"

    def save_config(self):
        """Create config for the actual mongod instances. Config is invalid until `self.bootstrap`
        succeeded, but can be created regardless.
        """
        write_file(self.conf_path, self.mongod_conf(secure=True))

    def bootstrap(self, users: list[MongoUser]):
        # perform health check and create directories
        if shutil.which("mongod") is None:
            fail("Missing: mongod (not on PATH)")
        if self.sock_path.exists():
            fail("MongoDB is already running. Stop mongod instance and try again!")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # keyfile is necessary with replica sets
        if not self.keyfile_path.exists():
            write_file(
                self.keyfile_path,
                base64.b64encode(os.urandom(756)).decode() + "\n",
                mode=0o400,
                exclusive=True,
            )

        bootstrap_log = self.dir / "bootstrap.log"
        bootstrap_conf = self.dir / "mongod-bootstap.conf"
        try:
            # start bootstrap
            write_file(bootstrap_conf, self.mongod_conf(secure=False))
            run(
                [
                    "mongod",
                    "--config",
                    str(bootstrap_conf),
                    "--fork",
                    "--logpath",
                    str(bootstrap_log),
                ]
            )

            wait_for(
                self.sock_path.exists,
                30,
                f"Socket {self.sock_path} did not appear. See {bootstrap_log}",
            )

            with MongoClient(
                self.uri(),
                directConnection=True,
                serverSelectionTimeoutMS=10000,
            ) as client:
                # initialize replica set
                try:
                    client.admin.command(
                        "replSetInitiate",
                        {
                            "_id": self.replica_set,
                            "members": [{"_id": 0, "host": f"127.0.0.1:{self.port}"}],
                        },
                    )
                except OperationFailure as e:
                    if e.code != 23:  # already initialized
                        raise

                # wait for replica set setup to finish
                wait_for(
                    lambda: client.admin.command("hello").get("isWritablePrimary", False),
                    60,
                    "MongoDB node did not become primary",
                )

                # ensure that there is at least one admin
                result = client.admin.command(
                    "usersInfo",
                    1,
                    filter={"roles": {"role": "root", "db": "admin"}},
                )
                has_admin = len(result.get("users", [])) > 0
                if has_admin:
                    print("Admin already exists")
                else:
                    admin = self.ask_admin_credentials()
                    self.create_user(client, admin.username, admin.password, ["root"])

                # add regular users
                for user in users:
                    self.create_user(
                        client,
                        user.username,
                        user.password,
                        [{"role": "readWrite", "db": user.db}],
                    )
        finally:
            # shotdown mongo bootstrap server and delete bootstrap config (just to be sure)
            subprocess.run(
                ["mongod", "--shutdown", "--dbpath", str(self.data_dir)],
                capture_output=True,
                text=True,
            )
            wait_for(lambda: not self.sock_path.exists(), 30)

        # only delete bootstrap config on sucess
        bootstrap_conf.unlink(missing_ok=True)
        return

    def mongod_conf(self, *, secure: bool) -> str:
        conf = textwrap.dedent(f"""\
        storage:
            dbPath: {self.data_dir}
        net:
            port: {self.port}
            bindIp: 127.0.0.1
            unixDomainSocket:
                enabled: true
                pathPrefix: {self.dir}
                filePermissions: 0700
        replication:
            replSetName: {self.replica_set}
        """)
        if secure:
            conf += textwrap.dedent(f"""\
            security:
                authorization: enabled
                keyFile: {self.dir / "keyfile"}
            """)
        return conf

    def uri(self) -> str:
        # pymongo treats any host containing "/" as a URI, so the socket path
        # has to be percent-encoded into a mongodb:// URI.
        return "mongodb://" + quote_plus(str(self.sock_path))

    @staticmethod
    def ask_admin_credentials() -> MongoAdmin:
        print(
            "Creating MongoDB admin credentials: "
            "(Admin credentials will not be saved. Make sure to remember them!)"
        )
        admin = input("Username: ")
        password = ask_password()
        return MongoAdmin(admin, password)

    @staticmethod
    def create_user(
        client: MongoClient[Any],
        name: str,
        password: SecretStr,
        roles: list[Any],
    ) -> None:
        try:
            client.admin.command("createUser", name, pwd=password.get_secret_value(), roles=roles)
        except OperationFailure as e:
            if e.code != 51003:  # user already exists
                raise


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


# --- sockets --------------------------------------------------------------------------------------

MAX_SOCKET_PATH = 100  # Unix socket paths are limited to ~104-108 bytes depending on the OS.


def check_sock_len(sock: Path):
    if len(str(sock)) > MAX_SOCKET_PATH:
        fail(f"Socket path too long ({len(str(sock))} chars): {sock}\n")


# --- prompts --------------------------------------------------------------------------------------


def ask_if_user_wants_to_continue(prompt: str) -> bool:
    VALID = {
        "": False,  # default is no
        "n": False,
        "no": False,
        "y": True,
        "ye": True,
        "yes": True,
    }
    print(prompt)
    inp = input("Continue anyway? [y/N]:")
    while VALID.get(inp.lower()) is None:
        inp = input("Invalid input. Try again: [y/N]")
    return VALID[inp.lower()]


def ask_password() -> SecretStr:
    while True:
        password = getpass(prompt="Password: ")
        if len(password) == 0:
            print("Password cannot be empty")
            continue

        repeat = getpass(prompt="Repeat Password: ")
        if password == repeat:
            break
        print("Passwords did not match. Please try again!")
    return SecretStr(password)


# --- util -----------------------------------------------------------------------------------------


def generate_password(n: int = 64) -> SecretStr:
    return SecretStr(secrets.token_urlsafe(n))


def write_file(path: Path, content: str, mode: int = 0o600, exclusive: bool = False) -> None:
    """Write a text file that is never group/world readable by accident."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, mode)


def wait_for(
    predicate: Callable[[], bool],
    timeout: float,
    timeout_error: str | None = None,
    interval: float = 0.5,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    if timeout_error is not None:
        fail(timeout_error)


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"Command failed: {' '.join(cmd)}\n{result.stdout}{result.stderr}")


def fail(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
