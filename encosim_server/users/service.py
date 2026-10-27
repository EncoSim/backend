import hashlib
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.sessions import SessionService
from encosim_server.core import errors
from encosim_server.core.security import utcnow
from encosim_server.core.settings import Settings
from encosim_server.database import CHARACTERS, TOMBSTONES, USERS
from encosim_server.users.models import UserInDB
from encosim_server.users.schemas import UserProfile


def identity_hash(sub: str) -> str:
    # TODO: use a keyed hash (HMAC with a dedicated pepper) instead of plain sha256
    return hashlib.sha256(sub.encode()).hexdigest()


class UserService:
    def __init__(self, settings: Settings, db: AsyncDatabase, sessions: SessionService):
        self.users = db[USERS]
        self.characters = db[CHARACTERS]
        self.tombstones = db[TOMBSTONES]
        self.sessions = sessions
        self.settings = settings

    async def get(self, user_id: UUID) -> UserInDB | None:
        raw = await self.users.find_one({"_id": user_id})
        if raw:
            return UserInDB.model_validate(raw)

    async def list(self) -> tuple[list[UserInDB], list[UUID]]:
        out = []
        corrupt = []
        async with self.users.find({}) as cursor:
            async for doc in cursor:
                try:
                    out.append(UserInDB.model_validate(doc))
                except ValidationError:
                    corrupt.append(UUID(hex=doc["_id"]))
        return out, corrupt

    async def upsert_login(
        self, login_id: str, profile: UserProfile
    ) -> Annotated[tuple[UserInDB, bool], errors.Raises[errors.Banned]]:
        """Find or create the user for a verified identity.
        Return (user in db, True on first login)

        Assumes `login_id` belongs to an authorized party!
        """
        res = await self.tombstones.find_one({"_id": login_id})
        if res:
            raise errors.Banned()

        is_admin = (
            profile.email_verified
            and profile.email is not None
            and profile.email in self.settings.auto_promote_to_admin
        )

        now = utcnow()
        new_id = uuid4()
        raw = await self.users.find_one_and_update(
            {"login_id": login_id},
            {
                "$set": {
                    "profile": profile.model_dump(),
                    "last_login": now,
                },
                "$setOnInsert": {
                    "login_id": login_id,
                    "_id": new_id,
                    "admin": is_admin,
                    "created_at": now,
                    "ban_status": {"banned": False, "number": 0},
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        user = UserInDB.model_validate(raw)
        if user.ban_status.banned:
            raise errors.Banned()
        is_new = user.user_id == new_id
        return user, is_new

    async def ban(self, user_id: UUID, reason: str) -> bool:
        res = await self.users.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "ban_status.banned": True,
                    "ban_status.reason": reason,
                    "ban_status.banned_at": utcnow(),
                },
                "$inc": {"ban_status.number": 1},
            },
        )
        if res.matched_count:
            await self.sessions.terminate_all_for_user(user_id)
        return bool(res.matched_count)

    async def unban(self, user_id: UUID) -> bool:
        res = await self.users.update_one(
            {"_id": user_id},
            {
                "$set": {"ban_status.banned": False},
                "$unset": {"ban_status.reason": "", "ban_status.banned_at": ""},
            },
        )
        return bool(res.matched_count)

    async def set_admin(self, user_id: UUID, is_admin: bool) -> bool:
        res = await self.users.update_one({"_id": user_id}, {"$set": {"admin": is_admin}})
        return bool(res.matched_count)

    async def delete(self, user_id: UUID) -> bool:
        """Hard-delete the user, their sessions and their characters.

        A banned user keeps a tombstone (hash of login id + reason)
        """
        await self.sessions.terminate_all_for_user(user_id)
        await self.characters.delete_many({"owner_id": user_id})
        user = await self.get(user_id)
        if user is None:
            return False
        if user.ban_status.banned:
            await self.tombstones.update_one(
                {"_id": user.login_id},
                {
                    "$set": {
                        "ban_status": user.ban_status.model_dump(),
                        "created_at": utcnow(),
                    }
                },
                upsert=True,
            )
        await self.users.delete_one({"_id": user_id})
        return True
