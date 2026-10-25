import asyncio

import uvicorn

from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.main import create_app, load_settings_from_env_file, preflight

ENV_FP = ".env.dev"


def create_dev():
    return create_app(
        settings=load_settings_from_env_file(ENV_FP),
        google=FakeGoogleVerifier(),
    )


def main():
    asyncio.run(preflight(load_settings_from_env_file(ENV_FP)))
    uvicorn.run(
        "encosim_server.scripts.dev:create_dev",
        factory=True,
        reload=True,
        reload_excludes=["tests/**/*"],
        reload_includes=[ENV_FP],
        reload_delay=2,
        host="localhost",
        port=8000,
    )


if __name__ == "__main__":
    main()
