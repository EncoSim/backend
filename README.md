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

## Simulation jobs

Celery uses Redis as its broker and MongoDB as its result backend. Celery remains the
source of truth for task state; the API treats non-terminal Celery states as pending. The `simulations` collection stores
the submitted configuration and the durable finished outcome for the user-facing history.
History is retained per user; `ENCOSIM_MAX_FINISHED_SIMULATIONS_PER_USER` defaults to `10`.

The simulation API exposes pending jobs under `/simu`, and finished records under
`/simu/history` (list), `/simu/history/{task_id}` (get), and `DELETE` on the same item path.
