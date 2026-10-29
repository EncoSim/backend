from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.auth.sessions import SessionService
from encosim_server.core import errors
from encosim_server.database import CHARACTERS, SESSIONS, TOMBSTONES, USERS
from encosim_server.users.models import UserInDB
from encosim_server.users.schemas import BanStatus
from encosim_server.users.service import UserService
from tests.conftest import Clock
from tests.helpers import CHROME_WIN, fake_token, ids_from_sessions

LoginUser = Callable[..., Awaitable[UserInDB]]


@pytest.fixture
def login_user(users: UserService, clock: Clock):
    # clock fixture is needed here, so monkeypatch applies before all logins
    _ = clock
    verify = FakeGoogleVerifier().verify

    async def login(
        sub: str = "alice",
        email: str | None = None,
        name: str | None = None,
        verified: bool = True,
    ):
        """Upsert a user the way the auth layer would after verifying Google."""
        login_id, profile = await verify(fake_token(sub=sub, email=email, name=name))
        profile.email_verified = verified
        user, _ = await users.upsert_login(login_id=login_id, profile=profile)
        return user

    return login


@pytest_asyncio.fixture
async def alice(login_user: LoginUser) -> UserInDB:
    return await login_user("alice")


@pytest_asyncio.fixture
async def bob(login_user: LoginUser) -> UserInDB:
    return await login_user("bob")


# --- upsert_login ------------------------------------------------------------------


async def test_first_login_creates_a_user_and_fixture_works(clock: Clock, alice: UserInDB):
    assert isinstance(alice.user_id, UUID)
    assert alice.login_id == "google:alice"
    assert alice.admin is False
    assert alice.ban_status.banned is False
    assert alice.profile.name == "alice"
    assert alice.profile.email == "alice@example.com"
    assert alice.created_at == clock()
    assert alice.last_login == clock()


async def test_repeat_login_returns_the_same_user_and_refreshes_profile(
    login_user: LoginUser, db: AsyncDatabase[Any], clock: Clock
):
    first = await login_user(sub="alice", name="Alice's old name")
    clock.tick(hours=1)

    second = await login_user(sub="alice", name="Alice's new name")

    assert second.user_id == first.user_id
    assert second.created_at == first.created_at
    assert second.last_login == clock()
    assert second.last_login > first.last_login
    assert second.profile.name == "Alice's new name"
    assert await db[USERS].count_documents({}) == 1


async def test_different_login_ids_are_different_users(alice: UserInDB, bob: UserInDB):
    assert alice.user_id != bob.user_id


async def test_login_does_not_reset_admin(users: UserService, login_user: LoginUser):
    user = await login_user(sub="alice")
    await users.set_admin(user.user_id, True)

    again = await login_user(sub="alice")

    assert again.admin is True


@pytest.mark.parametrize(
    "email, verified, expected",
    [
        ("admin@example.com", True, True),
        ("Admin@Example.com", True, True),  # case-insensitive
        ("admin@example.com", False, False),  # unverified email never promotes
        ("someone@example.com", True, False),
        ("someone@example.com", False, False),
    ],
)
async def test_auto_promotion_on_first_login(
    login_user: LoginUser, email: str, verified: bool, expected: bool
):
    user = await login_user(email=email, verified=verified)
    assert user.admin == expected


async def test_banned_user_cannot_log_in(login_user: LoginUser, users: UserService):
    user = await login_user("alice")
    assert await users.ban(user.user_id, "spam")

    with pytest.raises(errors.Banned):
        await login_user("alice")


async def test_tombstoned_identity_cannot_log_in(login_user: LoginUser, db: AsyncDatabase[Any]):
    await db[TOMBSTONES].insert_one(
        {"_id": "google:alice", "ban_status": BanStatus(banned=True, number=1).model_dump()}
    )

    with pytest.raises(errors.Banned):
        await login_user()

    assert await db[USERS].count_documents({}) == 0


# --- get -----------------------------------------------------------------------------


async def test_get(users: UserService, alice: UserInDB):
    res = await users.get(alice.user_id)
    assert res is not None
    assert res.user_id == alice.user_id
    assert await users.get(uuid4()) is None


# --- ban / unban -----------------------------------------------------------------------


async def test_ban_marks_user_and_ends_all_their_sessions(
    users: UserService, sessions: SessionService, clock: Clock, alice: UserInDB, bob: UserInDB
):
    await sessions.create(alice, CHROME_WIN)
    await sessions.create(alice, CHROME_WIN)
    _, bobs = await sessions.create(bob, CHROME_WIN)

    assert await users.ban(alice.user_id, "spam") is True

    banned = await users.get(alice.user_id)
    assert banned is not None
    assert banned.ban_status.banned is True
    assert banned.ban_status.reason == "spam"
    assert banned.ban_status.banned_at == clock()
    assert await sessions.list_user_sessions(alice.user_id) == []
    assert ids_from_sessions(await sessions.list_user_sessions(bob.user_id)) == {bobs}

    unbanned = await users.get(bob.user_id)
    assert unbanned is not None
    assert unbanned.ban_status.banned is False


async def test_ban_unknown_user(users: UserService):
    assert await users.ban(uuid4(), "x") is False


async def test_unban_restores_the_user(users: UserService, login_user: LoginUser):
    user = await login_user()
    await users.ban(user.user_id, "spam")

    assert await users.unban(user.user_id) is True

    restored = await users.get(user.user_id)
    assert restored is not None
    assert restored.ban_status.banned is False
    assert restored.ban_status.reason is None and restored.ban_status.banned_at is None
    await login_user()  # may log in again


async def test_unban_unknown_user(users: UserService):
    assert await users.unban(uuid4()) is False


# --- roles ------------------------------------------------------------------------------


async def test_set_admin(users: UserService, alice: UserInDB):
    assert await users.set_admin(alice.user_id, True) is True
    res = await users.get(alice.user_id)
    assert res is not None
    assert res.admin is True


async def test_set_admin_unknown_user(users: UserService):
    assert await users.set_admin(uuid4(), True) is False


# --- delete -----------------------------------------------------------------------------


async def test_delete_removes_user_sessions_and_characters_but_nobody_elses(
    users: UserService,
    sessions: SessionService,
    db: AsyncDatabase[Any],
    alice: UserInDB,
    bob: UserInDB,
):
    await sessions.create(alice, CHROME_WIN)
    _, bobs = await sessions.create(bob, CHROME_WIN)
    await db[CHARACTERS].insert_many(
        [{"_id": uuid4(), "owner_id": alice.user_id}, {"_id": uuid4(), "owner_id": alice.user_id}]
    )
    bobs_character = uuid4()
    await db[CHARACTERS].insert_one({"_id": bobs_character, "owner_id": bob.user_id})

    assert await users.delete(alice.user_id) is True

    assert await users.get(alice.user_id) is None
    assert await db[SESSIONS].count_documents({"user_id": alice.user_id}) == 0
    assert await db[CHARACTERS].count_documents({"owner_id": alice.user_id}) == 0
    assert await users.get(bob.user_id) is not None
    assert ids_from_sessions(await sessions.list_user_sessions(bob.user_id)) == {bobs}
    assert await db[CHARACTERS].count_documents({"_id": bobs_character}) == 1


async def test_delete_unknown_user(users: UserService):
    assert await users.delete(uuid4()) is False


async def test_deleting_a_normal_user_leaves_no_tombstone_and_allows_signing_up_again(
    users: UserService, db: AsyncDatabase[Any], login_user: LoginUser
):
    first = await login_user()

    await users.delete(first.user_id)

    assert await db[TOMBSTONES].count_documents({}) == 0
    again = await login_user()
    assert again.user_id != first.user_id


async def test_deleting_a_banned_user_blocks_the_identity(
    users: UserService, db: AsyncDatabase[Any], clock: Clock, login_user: LoginUser
):
    user = await login_user()
    await users.ban(user.user_id, "spam")

    assert await users.delete(user.user_id) is True

    tombstone = await db[TOMBSTONES].find_one({"_id": "google:alice"})
    assert tombstone is not None
    assert tombstone["created_at"] == clock()
    ban = BanStatus.model_validate(tombstone["ban_status"])
    assert ban.banned
    assert ban.reason == "spam"
    assert ban.number == 1
    assert ban.banned_at == clock()
    with pytest.raises(errors.Banned):
        await login_user()  # cannot evade the ban by deleting and re-registering
