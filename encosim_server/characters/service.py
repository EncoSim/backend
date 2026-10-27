import logging
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.characters.models import CharInDB
from encosim_server.characters.schema import CharDownload, CharUpload
from encosim_server.core import errors
from encosim_server.core.security import utcnow
from encosim_server.core.settings import Settings
from encosim_server.database import CHARACTERS

logger = logging.getLogger(__name__)

class CharacterService:
    def __init__(self, settings: Settings, db: AsyncDatabase) -> None:
        self.settings = settings
        self.chars = db[CHARACTERS]

    async def get(
        self, char_id: UUID, owner_id: UUID
    ) -> Annotated[CharInDB, errors.Raises[errors.NotFound]]:
        doc = await self.chars.find_one({"_id": char_id, "owner_id": owner_id})
        if doc is None:
            raise errors.NotFound()
        return CharInDB.model_validate(doc)

    # TODO: step-wise queries
    async def list(self, owner_id: UUID) -> list[CharInDB]:
        found = []
        async with self.chars.find({"owner_id": owner_id}) as cursor:
            async for doc in cursor:
                try:
                    found.append(CharInDB.model_validate(doc))
                except ValidationError as e:
                    logger.error(f"Corrupted character {doc['_id']}", exc_info=e)
        return found

    def count_owned(self, owner_id: UUID):
        return self.chars.count_documents({"owner_id": owner_id})

    async def create(
        self, owner_id: UUID, payload: CharUpload, max_number: int | None = None
    ) -> Annotated[CharInDB, errors.Raises[errors.TooManyCharacters]]:
        now = utcnow()
        char = CharInDB(
            char_id=uuid4(),
            owner_id=owner_id,
            created_at=now,
            modified_at=now,
            version=0,
            data=payload.data,
        )
        if max_number is None:
            max_number = self.settings.max_characters
        async with self.chars.database.client.start_session() as session:
            async with await session.start_transaction():
                n = await self.chars.count_documents({"owner_id": owner_id}, session=session)
                if n >= max_number:
                    raise errors.TooManyCharacters(errors.AllowedNumberDetail(allowed=max_number))
                await self.chars.insert_one(char.model_dump(by_alias=True), session=session)
        return char

    async def update(
        self, char_id: UUID, owner_id: UUID, payload: CharUpload
    ) -> Annotated[CharInDB, errors.Raises[errors.NotFound, errors.CharacterOutdated]]:
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
        raise errors.CharacterOutdated(detail=payload)

    async def delete(
        self, char_id: UUID, owner_id: UUID
    ) -> Annotated[CharInDB, errors.Raises[errors.NotFound]]:
        doc = await self.chars.find_one_and_delete({"_id": char_id, "owner_id": owner_id})
        if doc is None:
            raise errors.NotFound()
        return CharInDB.model_validate(doc)
