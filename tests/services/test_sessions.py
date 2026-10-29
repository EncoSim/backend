from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.schema import SessionSchema
from encosim_server.auth.sessions import SessionService, get_session_info
from encosim_server.core import errors
from encosim_server.core.security import new_refresh_token, split_refresh_token
from encosim_server.core.settings import Settings
from encosim_server.database import SESSIONS
from encosim_server.users.models import UserInDB
from encosim_server.users.schemas import BanStatus, UserProfile
from tests.conftest import Clock
from tests.helpers import CHROME_WIN, FIREFOX_LINUX, ids_from_sessions, tamper_refresh_token

MakeUser = Callable[..., UserInDB]


@pytest.fixture
def make_user(clock: Clock):
    """Upsert a user the way the auth layer would after verifying Google."""

    def new(name: str = "alice", banned: bool = False) -> UserInDB:
        """create a unique fake user"""
        return UserInDB(
            login_id=f"google:{uuid4()}",
            _id=uuid4(),
            last_login=clock(),
            created_at=clock(),
            ban_status=BanStatus(banned=banned),
            admin=False,
            profile=UserProfile(
                name=name, email=f"{name}@gmail.com", email_verified=True, picture=None
            ),
        )

    return new


@pytest.fixture
def alice(make_user: MakeUser):
    return make_user(name="alice")


@pytest.fixture
def bob(make_user: MakeUser):
    return make_user(name="bob")


# --- get_session_info ----------------------------------------------------------


def test_session_info_names_browser_and_os():
    info = get_session_info(CHROME_WIN)
    assert "Chrome" in info and "Windows" in info


def test_session_info_is_a_readable_label_not_an_object_repr():
    for ua in (CHROME_WIN, FIREFOX_LINUX, "", "totally-unknown-agent"):
        info = get_session_info(ua)
        assert isinstance(info, str) and info
        assert "family=" not in info and "(" not in info


# --- create ----------------------------------------------------------------------


async def test_create_returns_token_and_id_and_stores_only_a_hash(
    sessions: SessionService, db: AsyncDatabase[Any], alice: UserInDB
):
    token, session_id = await sessions.create(alice, CHROME_WIN)

    assert isinstance(session_id, UUID)
    doc = await db[SESSIONS].find_one({"_id": session_id})
    assert doc is not None
    assert doc["user_id"] == alice.user_id
    assert token not in repr(doc)  # the raw token is never stored
    assert doc["refresh_hash"]
    assert "Chrome" in doc["info"]


async def test_new_session_is_listed_immediately(alice: UserInDB, sessions: SessionService):
    _, session_id = await sessions.create(alice, CHROME_WIN)

    listed = await sessions.list_user_sessions(alice.user_id)
    assert len(listed) > 0

    found: SessionSchema | None = None
    for s in listed:
        if s.session_id == session_id:
            found = s
            break

    assert found is not None
    assert "Chrome" in found.info


async def test_session_limit_evicts_the_least_recently_used(
    alice: UserInDB, sessions: SessionService, clock: Clock
):
    ids = []
    # limit is 3 (from pinned settings)
    for _ in range(3):
        token, sid = await sessions.create(alice, CHROME_WIN)
        ids.append((token, sid))
        clock.tick(seconds=1)

    # session 0 is used => session 1 is now the least recently used
    await sessions.rotate(ids[0][0])
    clock.tick(seconds=1)
    _, newest = await sessions.create(alice, FIREFOX_LINUX)

    remaining = {s.session_id for s in await sessions.list_user_sessions(alice.user_id)}
    assert remaining == {ids[0][1], ids[2][1], newest}


async def test_session_limit_is_per_user(
    alice: UserInDB, bob: UserInDB, sessions: SessionService, clock: Clock
):
    for _ in range(3):
        await sessions.create(alice, CHROME_WIN)
        clock.tick(seconds=1)
    await sessions.create(bob, CHROME_WIN)

    assert len(await sessions.list_user_sessions(alice.user_id)) == 3
    assert len(await sessions.list_user_sessions(bob.user_id)) == 1


# --- rotate -------------------------------------------------------------------


async def test_token_parsing():
    session_id = uuid4()
    token, hash = new_refresh_token(session_id)
    parsed = split_refresh_token(token)
    assert parsed is not None
    assert (session_id, hash) == parsed


async def test_rotate_issues_a_new_token_and_extends_ttl(
    alice: UserInDB,
    sessions: SessionService,
    db: AsyncDatabase[Any],
    clock: Clock,
    settings: Settings,
):
    token, sid = await sessions.create(alice, CHROME_WIN)
    clock.tick(days=1)
    print(settings.session_ttl)
    new_token, _, _ = await sessions.rotate(token)
    expires_at = clock() + timedelta(days=30)

    assert new_token != token
    doc = await db[SESSIONS].find_one({"_id": sid})
    assert doc is not None
    assert doc["expires_at"] == expires_at
    assert doc["last_used_at"] == clock()


async def test_rotated_token_can_be_rotated_again(alice: UserInDB, sessions: SessionService):
    token, _ = await sessions.create(alice, CHROME_WIN)
    second, _, _ = await sessions.rotate(token)
    third, _, _ = await sessions.rotate(second)
    assert len({token, second, third}) == 3


async def test_replaying_an_old_token_is_rejected_and_kills_the_session(
    alice: UserInDB, sessions: SessionService
):
    old, sid = await sessions.create(alice, CHROME_WIN)
    new, _, _ = await sessions.rotate(old)

    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(old)  # replay of the previous token

    assert await sessions.list_user_sessions(alice.user_id) == []
    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(new)  # the legitimate holder is logged out too


async def test_wrong_secret_is_rejected_and_kills_the_session(
    alice: UserInDB, sessions: SessionService
):
    token, _ = await sessions.create(alice, CHROME_WIN)

    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(tamper_refresh_token(token))

    assert await sessions.list_user_sessions(alice.user_id) == []


@pytest.mark.parametrize("garbage", ["", "garbage", "no-dot-here", "....", "not-a-uuid.secret"])
async def test_malformed_token_is_rejected(sessions: SessionService, garbage: str):
    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(garbage)


async def test_token_for_unknown_session_is_rejected(sessions: SessionService):
    token, _ = new_refresh_token(uuid4())
    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(token)


@pytest.mark.usefixtures("disable_ttl_cleanup")
async def test_expired_session_cannot_be_rotated(
    alice: UserInDB, sessions: SessionService, db: AsyncDatabase[Any], clock: Clock
):
    """Mongo's TTL monitor only runs about once a minute, so expired documents can linger.

    Drop the TTL index here so the document stays around deterministically
    (mongomock would otherwise purge it immediately, a real server only after a while).
    """
    token, sid = await sessions.create(alice, CHROME_WIN)
    expired = clock() - timedelta(minutes=1)
    await db[SESSIONS].update_one({"_id": sid}, {"$set": {"expires_at": expired}})

    with pytest.raises(errors.InvalidSession):
        await sessions.rotate(token)

    doc = await db[SESSIONS].find_one({"_id": sid})
    assert doc is not None
    assert doc["expires_at"] == expired  # and it was not extended


# --- terminate -----------------------------------------------------------------------


async def test_terminate_removes_only_that_session(alice: UserInDB, sessions: SessionService):
    _, a = await sessions.create(alice, CHROME_WIN)
    _, b = await sessions.create(alice, FIREFOX_LINUX)

    await sessions.terminate(a)

    assert ids_from_sessions(await sessions.list_user_sessions(alice.user_id)) == {b}


async def test_terminate_unknown_session_is_a_noop(sessions: SessionService):
    await sessions.terminate(uuid4())


async def test_terminate_by_token_requires_the_current_token(
    alice: UserInDB, sessions: SessionService
):
    old, sid = await sessions.create(alice, CHROME_WIN)
    current, _, _ = await sessions.rotate(old)

    await sessions.terminate_by_token(old)  # stale token: ignored
    assert ids_from_sessions(await sessions.list_user_sessions(alice.user_id)) == {sid}

    await sessions.terminate_by_token(tamper_refresh_token(current))  # wrong secret: ignored
    assert ids_from_sessions(await sessions.list_user_sessions(alice.user_id)) == {sid}

    await sessions.terminate_by_token(current)
    assert await sessions.list_user_sessions(alice.user_id) == []


@pytest.mark.parametrize("garbage", ["", "garbage", "not-a-uuid.secret"])
async def test_terminate_by_token_is_silent_for_garbage(sessions: SessionService, garbage: str):
    await sessions.terminate_by_token(garbage)


async def test_terminate_for_user_checks_ownership(
    alice: UserInDB, bob: UserInDB, sessions: SessionService
):
    _, alice_session = await sessions.create(alice, CHROME_WIN)
    _, bob_session = await sessions.create(bob, CHROME_WIN)

    assert await sessions.terminate_for_user(bob.user_id, alice_session) is False
    assert ids_from_sessions(await sessions.list_user_sessions(alice.user_id)) == {alice_session}

    assert await sessions.terminate_for_user(alice.user_id, alice_session) is True
    assert await sessions.terminate_for_user(alice.user_id, alice_session) is False  # already gone


async def test_terminate_all_for_user_leaves_other_users_alone(
    alice: UserInDB, bob: UserInDB, sessions: SessionService
):
    await sessions.create(alice, CHROME_WIN)
    await sessions.create(alice, FIREFOX_LINUX)
    _, bobs = await sessions.create(bob, CHROME_WIN)

    await sessions.terminate_all_for_user(alice.user_id)

    assert await sessions.list_user_sessions(alice.user_id) == []
    assert ids_from_sessions(await sessions.list_user_sessions(bob.user_id)) == {bobs}


# --- list_user_session ------------------------------------------------------------------


async def test_list_is_sorted_by_last_use_and_skips_expired(
    alice: UserInDB, sessions: SessionService, db: AsyncDatabase[Any], clock: Clock
):
    t_a, a = await sessions.create(alice, CHROME_WIN)
    clock.tick(seconds=1)
    _, b = await sessions.create(alice, FIREFOX_LINUX)
    clock.tick(seconds=1)
    _, c = await sessions.create(alice, CHROME_WIN)
    clock.tick(seconds=1)
    await sessions.rotate(t_a)  # a becomes the most recently used
    await db[SESSIONS].update_one(
        {"_id": b}, {"$set": {"expires_at": clock() - timedelta(seconds=1)}}
    )

    listed = await sessions.list_user_sessions(alice.user_id)

    assert [s.session_id for s in listed] == [a, c]
    assert all(s.expires_at > clock() for s in listed)


async def test_list_for_user_without_sessions_is_empty(alice: UserInDB, sessions: SessionService):
    assert await sessions.list_user_sessions(alice.user_id) == []
