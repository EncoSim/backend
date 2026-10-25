import logging
from typing import TypeVar
from uuid import UUID, uuid4

from encosim.components.shape_changing_component import BaseModel
from fastapi import HTTPException
from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.asynchronous.cursor import AsyncCursor
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.characters.models import CharInDB
from encosim_server.characters.schema import CharDownload, CharUpload
from encosim_server.core.security import utcnow
from encosim_server.core.settings import Settings
from encosim_server.database import CHARACTERS

logger = logging.getLogger(__name__)

MISSING = HTTPException(404, "character not found")


class CharacterService:
    def __init__(self, settings: Settings, db: AsyncDatabase) -> None:
        self.settings = settings
        self.chars = db[CHARACTERS]

    async def get(self, char_id: UUID, owner_id: UUID):
        doc = await self.chars.find_one({"_id": char_id, "owner_id": owner_id})
        if doc is None:
            raise MISSING
        return CharInDB.model_validate(doc)

    # TODO: step-wise queries
    async def list(self, owner_id: UUID):
        async with self.chars.find({"owner_id": owner_id}) as cursor:
            return [x async for x in self._iterate_valid(cursor)]

    T = TypeVar("T", bound=BaseModel)

    async def _iterate_valid(self, cursor: AsyncCursor, validator: type[T] = CharInDB):
        async for doc in cursor:
            try:
                char = validator.model_validate(doc)
            except ValidationError as e:
                logger.debug("Parsing character {%s} failed: %s", doc.get("_id"), e)
                continue
            yield char

    def count_owned(self, owner_id: UUID):
        return self.chars.count_documents({"owner_id": owner_id})

    async def create(self, owner_id: UUID, payload: CharUpload):
        """does NOT perform count check, because that depends on user priviledges"""
        now = utcnow()
        char = CharInDB(
            char_id=uuid4(),
            owner_id=owner_id,
            created_at=now,
            modified_at=now,
            version=0,
            data=payload.data,
        )
        await self.chars.insert_one(char.model_dump(by_alias=True))
        return char

    async def update(self, char_id: UUID, owner_id: UUID, payload: CharUpload):
        doc = await self.chars.find_one_and_update(
            {"_id": char_id, "owner_id": owner_id, "version": payload.version},
            {
                "$inc": {"version": 1},
                "$set": {
                    "data": payload.data.model_dump(),
                    "modified_at": utcnow(),
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return CharInDB.model_validate(doc)
        # either we did the update or send back the most recent version
        # no need for a transaction: we either write or read, never both.
        real = await self.get(char_id, owner_id)
        # TODO: how to document errors cleanly?
        payload = CharDownload.model_validate(real, from_attributes=True)
        raise HTTPException(409, payload.model_dump(mode="json"))

    async def delete(self, char_id: UUID, owner_id: UUID):
        res = await self.chars.delete_one({"_id": char_id, "owner_id": owner_id})
        if res.deleted_count == 0:
            raise MISSING
