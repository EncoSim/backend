# Encosim Server Backend

## Tests

```sh
uv run pytest
```

Needs `.env.test` (see `bootstrap.py`), a running `mongod` and `redis-server` on `PATH`.
The suite starts its own Redis and Celery worker (separate processes, killed afterwards);
the Redis from `.env.test` is not used.
