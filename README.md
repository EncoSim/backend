# EncoSim backend

## Running

```sh
uv run encosim dev      # redis + celery worker + backend, reloading on changes and crashes
uv run encosim serve    # the backend only (add --reload during development)
uv run encosim worker   # a celery worker
```

All commands expect `--config path/to/config.json` to load the application settings and a running `mongod`.

`dev` expects a running `mongod` and `redis-server` on `PATH`; it starts redis itself.
Production runs under `supervisord` (see `bootstrap`).

Logs go to the terminal and, if `logs_dir` is set, to `<logs_dir>/<service>/<service>.log`
with `main` for the process that started the others, then `backend`, `celery` and `redis`.

## Tests

```sh
uv run pytest
```

Needs `config/test.json` (generate with `uv run bootstrap`, then create a symlink), a running `mongod` and `redis-server` on `PATH`.
The test suite starts its own redis and celery worker. The redis socket in the config will be ignored.

## MongoDB

The app expects `mongod` to be managed externally (for example via `supervisord`) and requires a replica set.
