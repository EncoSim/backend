# EncoSim backend

## Running

```sh
uv run encosim dev      # redis + celery worker + backend, reloading on changes and crashes
uv run encosim serve    # the backend only (add --reload during development)
uv run encosim worker   # a celery worker
```

All commands read their settings from `--env-file`, else `$ENCOSIM_ENV_FILE`, else `./.env`.
`uv run dev`, `uv run main` and `uv run celery` still work as aliases.

`dev` expects a running `mongod` and `redis-server` on `PATH`; it starts redis itself.
Production runs under supervisord (see `bootstrap`), not under `dev`.

Logs go to the terminal and, if `logs_dir` is set, to `<logs_dir>/<service>/<service>.log`
with `main` for the process that started the others, then `backend`, `celery` and `redis`.

## Tests

```sh
uv run pytest
```

Needs `.env.test` (see `bootstrap`), a running `mongod` and `redis-server` on `PATH`.
The suite starts its own redis and celery worker with the same supervisor as `dev`
(in a scratch directory, removed afterwards); the redis from `.env.test` is not used.
