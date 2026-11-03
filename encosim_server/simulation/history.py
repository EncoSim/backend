import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from encosim_server.database import SIMULATIONS

logger = logging.getLogger(__name__)


def finalize_simulation(
    backend: Any,
    task_id: str,
    owner_id: UUID,
    *,
    status: str,
    result: Any,
    summary: str,
    error_type: str | None,
    limit: int,
) -> bool:
    """Write the first terminal outcome and enforce per-user history retention.

    This is deliberately synchronous: Celery's MongoDB backend already owns a synchronous
    PyMongo client, and task hooks run in synchronous worker processes. API callers invoke
    this helper in a threadpool.
    """
    collection = backend.database[SIMULATIONS]
    written = collection.update_one(
        {"_id": task_id, "owner_id": owner_id, "status": {"$exists": False}},
        {
            "$set": {
                "status": status,
                "result": result,
                "summary": summary,
                "error_type": error_type,
                "finished_at": datetime.now(timezone.utc),
            }
        },
    )
    if not written.modified_count:
        return False

    old = list(
        collection.find(
            {"owner_id": owner_id, "status": {"$in": ["success", "failed"]}},
            {"_id": 1},
        )
        .sort([("finished_at", -1), ("_id", -1)])
        .skip(limit)
    )
    if old:
        old_ids = [doc["_id"] for doc in old]
        collection.delete_many({"_id": {"$in": old_ids}})
        for old_id in old_ids:
            try:
                backend.forget(old_id)
            except Exception:
                logger.exception("Could not remove expired Celery result %s", old_id)
    return True
