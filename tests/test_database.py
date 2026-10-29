from typing import Any
from uuid import uuid4

import pytest
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from encosim_server.database import (
    SESSIONS,
    USERS,
    create_indexes,
)


async def test_login_id_is_unique(db: AsyncDatabase[Any]):
    await db[USERS].insert_one({"_id": uuid4(), "login_id": "google:x"})
    with pytest.raises(DuplicateKeyError):
        await db[USERS].insert_one({"_id": uuid4(), "login_id": "google:x"})
        print(await db[USERS].index_information())


async def test_sessions_expire_via_ttl_index(db: AsyncDatabase[Any]):
    info = await db[SESSIONS].index_information()
    ttl = [i for i in info.values() if "expireAfterSeconds" in i]
    assert len(ttl) == 1
    assert ttl[0]["expireAfterSeconds"] == 0
    assert dict(ttl[0]["key"]) == {"expires_at": 1}


async def test_create_indexes_is_idempotent(db: AsyncDatabase[Any]):
    await create_indexes(db)
    await create_indexes(db)
