from uuid import UUID, uuid4

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from ua_parser import parse

from encosim_server.auth.exceptions import INVALID_SESSION
from encosim_server.auth.schema import SessionSchema
from encosim_server.core.security import new_refresh_token, split_refresh_token, utcnow
from encosim_server.core.settings import Settings
from encosim_server.database import SESSIONS, USERS
from encosim_server.users.models import UserInDB


class SessionService:
    def __init__(self, settings: Settings, db: AsyncDatabase):
        self.settings = settings
        self.sessions = db[SESSIONS]
        self.users = db[USERS]

    async def create(self, user: UserInDB, user_agent: str | None) -> tuple[str, UUID]:
        """Returns (refresh token, session id)."""
        now = utcnow()
        session_id = uuid4()
        token, token_hash = new_refresh_token(session_id)
        await self.sessions.insert_one(
            {
                "_id": session_id,
                "user_id": user.user_id,
                "info": get_session_info(user_agent),
                "refresh_hash": token_hash,
                "last_used_at": now,
                "expires_at": now + self.settings.session_ttl,
            }
        )
        await self._enforce_limit(user.user_id)
        return token, session_id

    async def _enforce_limit(self, user_id: UUID) -> None:
        cursor = (
            self.sessions.find({"user_id": user_id})
            .sort("last_used_at", -1)
            .skip(self.settings.max_sessions_per_user)
        )
        for doc in await cursor.to_list(length=None):
            await self.terminate(doc["_id"])

    async def rotate(self, refresh_token: str) -> tuple[str, UUID, UUID]:
        """Swap the refresh token for a new one. Returns (new refresh token, session id, user id).

        TODO: optional grace window for legitimate concurrent refreshes.
        """
        now = utcnow()
        parsed = split_refresh_token(refresh_token)
        if parsed is None:
            raise INVALID_SESSION
        session_id, old_hash = parsed
        new_token, new_hash = new_refresh_token(session_id)
        expires_at = now + self.settings.session_ttl
        doc = await self.sessions.find_one_and_update(
            {"_id": session_id, "expires_at": {"$gt": now}},
            {
                "$set": {
                    "refresh_hash": new_hash,
                    "last_used_at": now,
                    "expires_at": expires_at,
                }
            },
            return_document=ReturnDocument.BEFORE,
        )
        if doc is None:  # no session
            raise INVALID_SESSION
        if doc.get("refresh_hash") != old_hash:  # wrong hash => reuse detected
            await self.terminate(session_id)
            raise INVALID_SESSION
        return new_token, doc["_id"], doc["user_id"]

    async def terminate(self, session_id: UUID) -> None:
        await self.sessions.delete_one({"_id": session_id})

    async def terminate_by_token(self, refresh_token: str):
        """Revoke the session a (current) refresh token belongs to. Silent if invalid."""
        parsed = split_refresh_token(refresh_token)
        if parsed is None:
            return
        session_id, hashed_secret = parsed
        await self.sessions.delete_one({"_id": session_id, "refresh_hash": hashed_secret})

    async def terminate_for_user(self, user_id: UUID, session_id: UUID) -> bool:
        res = await self.sessions.delete_one({"_id": session_id, "user_id": user_id})
        return res.deleted_count == 1

    async def terminate_all_for_user(self, user_id: UUID):
        await self.sessions.delete_many({"user_id": user_id})

    async def list_user_session(self, user_id: UUID) -> list[SessionSchema]:
        cursor = self.sessions.find(
            {"user_id": user_id, "expires_at": {"$gt": utcnow()}},
        ).sort("last_used_at", -1)
        return [SessionSchema.model_validate(raw) async for raw in cursor]

    async def is_valid(self, user_id: UUID, session_id: UUID) -> bool:
        res = await self.sessions.find_one(
            {
                "_id": session_id,
                "user_id": user_id,
                "expires_at": {"$gt": utcnow()},
            }
        )
        return res is not None


def get_session_info(user_agent_string: str | None) -> str:
    if user_agent_string is None:
        return "Unknown Browser on Unknown OS"
    res = parse(user_agent_string)
    browser = getattr(res.user_agent, "family", "Unknown Browser")
    os = getattr(res.os, "family", "Unknown OS")
    return f"{browser} on {os}"
