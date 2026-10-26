"""Tests for AuthService (DRAFT, not run).

Everything marked ADAPT depends on code I have not seen:
  * StubGoogle: I assume `GoogleVerifier.verify()` returns `(login_id, UserProfile)`
  * claims_of(): how your access token is decoded and what its claims are called
  * the `ban_status.banned` field name used for the raw ban
  * where UserProfile / the exceptions live
"""

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.service import AuthService
from encosim_server.auth.sessions import SessionService
from encosim_server.core import exceptions
from encosim_server.core.security import decode_access_token
from encosim_server.core.settings import Settings
from encosim_server.database import SESSIONS, USERS
from encosim_server.resources import Resources
from encosim_server.users.service import UserService
from tests.helpers import CHROME_WIN, FIREFOX_LINUX, ids_from_sessions

# --- helpers -----------------------------------------------------------------------------------


def fake_token(sub: str = "alice") -> str:
    """google id token for fake verifier"""
    return f"fake:{sub}:{sub}@example.com"


async def login(auth: AuthService, sub: str = "alice", user_agent: str | None = CHROME_WIN):
    return await auth.login_google(fake_token(sub), user_agent)

# --- login_google ---------------------------------------------------------------------------------


async def test_login_creates_user_session_and_matching_tokens(resources: Resources):
    result = await login(resources.auth, sub="alice")

    assert result.user.login_id == "google:alice"
    assert await resources.users.get(result.user.user_id) is not None

    claims = decode_access_token(resources.settings, result.access_token)
    assert claims.sub == result.user.user_id
    assert ids_from_sessions(await resources.sessions.list_user_session(claims.sub)) == {claims.sid}

    # the refresh token belongs to that very session
    _, rotated_session_id, rotated_user_id = await resources.sessions.rotate(result.refresh_token)
    assert (rotated_session_id, rotated_user_id) == (claims.sid, claims.sub)


async def test_login_records_the_device(auth: AuthService, sessions: SessionService):
    result = await login(auth, user_agent=CHROME_WIN)

    listed = await sessions.list_user_session(result.user.user_id)

    assert "Chrome" in listed[0].info


async def test_login_without_a_user_agent_header(auth: AuthService, sessions: SessionService):
    result = await login(auth, user_agent=None)

    assert len(await sessions.list_user_session(result.user.user_id)) == 1


async def test_each_login_is_a_separate_session(auth: AuthService, sessions: SessionService):
    first = await login(auth, user_agent=CHROME_WIN)
    second = await login(auth, user_agent=FIREFOX_LINUX)

    assert first.user.user_id == second.user.user_id
    assert first.refresh_token != second.refresh_token
    assert len(await sessions.list_user_session(first.user.user_id)) == 2


async def test_different_google_accounts_are_different_users(auth: AuthService):
    alice = await login(auth, "alice")
    bob = await login(auth, "bob")

    assert alice.user.user_id != bob.user.user_id


async def test_invalid_google_token_creates_nothing(auth: AuthService, db: AsyncDatabase):
    with pytest.raises(exceptions.InvalidGoogleToken):
        await auth.login_google("not-a-valid-token", CHROME_WIN)
    assert await db[USERS].count_documents({}) == 0
    assert await db[SESSIONS].count_documents({}) == 0


async def test_banned_user_cannot_log_in(
    auth: AuthService, users: UserService, sessions: SessionService
):
    result = await login(auth)
    await users.ban(result.user.user_id, "spam")

    with pytest.raises(exceptions.Banned):
        await login(auth)

    assert await sessions.list_user_session(result.user.user_id) == []  # no new session either


# --- refresh --------------------------------------------------------------------------------------


async def test_refresh_rotates_the_token_and_keeps_user_and_session(
    auth: AuthService, settings: Settings
):
    first = await login(auth)
    claims = decode_access_token(settings, first.access_token)

    refreshed = await auth.refresh(first.refresh_token)

    assert refreshed.refresh_token != first.refresh_token
    assert refreshed.user.user_id == claims.sub
    refreshed_claims = decode_access_token(settings, refreshed.access_token)
    assert (refreshed_claims.sub, refreshed_claims.sid) == (claims.sub, claims.sid)


async def test_refreshed_token_can_be_refreshed_again(auth: AuthService):
    first = await login(auth)
    second = await auth.refresh(first.refresh_token)
    third = await auth.refresh(second.refresh_token)

    assert len({first.refresh_token, second.refresh_token, third.refresh_token}) == 3


async def test_replaying_the_old_refresh_token_fails_and_ends_the_session(
    auth: AuthService, sessions: SessionService
):
    first = await login(auth)
    refreshed = await auth.refresh(first.refresh_token)

    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(first.refresh_token)  # replay

    assert await sessions.list_user_session(first.user.user_id) == []
    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(refreshed.refresh_token)  # the legitimate holder is logged out too


@pytest.mark.parametrize("garbage", ["", "garbage", "not-a-uuid.secret"])
async def test_refresh_with_a_garbage_token_is_rejected(auth: AuthService, garbage):
    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(garbage)


async def test_refreshing_one_session_does_not_touch_the_others(
    auth: AuthService, sessions: SessionService
):
    laptop = await login(auth, user_agent=CHROME_WIN)
    phone = await login(auth, user_agent=FIREFOX_LINUX)

    await auth.refresh(laptop.refresh_token)

    assert len(await sessions.list_user_session(laptop.user.user_id)) == 2
    await auth.refresh(phone.refresh_token)  # still valid


async def test_banned_user_cannot_refresh_and_loses_all_sessions(
    auth: AuthService, db: AsyncDatabase, sessions: SessionService
):
    laptop = await login(auth, user_agent=CHROME_WIN)
    await login(auth, user_agent=FIREFOX_LINUX)
    user_id = laptop.user.user_id
    # flag the user directly: users.ban() would already end the sessions, hiding this code path
    await db[USERS].update_one({"_id": user_id}, {"$set": {"ban_status.banned": True}})  # ADAPT

    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(laptop.refresh_token)

    assert await sessions.list_user_session(user_id) == []


async def test_banning_through_the_user_service_stops_refresh(
    auth: AuthService, users: UserService
):
    first = await login(auth)
    await users.ban(first.user.user_id, "spam")

    with pytest.raises(exceptions.InvalidSession) as exc:
        await auth.refresh(first.refresh_token)

    assert exc.value.status_code == 401


async def test_refresh_for_a_vanished_user_is_rejected_and_cleans_up(
    auth: AuthService, db: AsyncDatabase, sessions: SessionService
):
    first = await login(auth)
    await db[USERS].delete_one({"_id": first.user.user_id})  # user gone, session left behind

    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(first.refresh_token)

    assert await sessions.list_user_session(first.user.user_id) == []


async def test_unbanned_user_can_log_in_again(auth: AuthService, users: UserService):
    first = await login(auth)
    await users.ban(first.user.user_id, "spam")
    await users.unban(first.user.user_id)

    again = await login(auth)

    assert again.user.user_id == first.user.user_id


# --- logout ---------------------------------------------------------------------------------------


async def test_logout_ends_the_session(auth: AuthService, sessions: SessionService):
    result = await login(auth)

    await auth.logout(result.refresh_token)

    assert await sessions.list_user_session(result.user.user_id) == []
    with pytest.raises(exceptions.InvalidSession):
        await auth.refresh(result.refresh_token)


async def test_logout_leaves_other_sessions_alone(auth: AuthService, sessions: SessionService):
    laptop = await login(auth, user_agent=CHROME_WIN)
    phone = await login(auth, user_agent=FIREFOX_LINUX)

    await auth.logout(laptop.refresh_token)

    assert len(await sessions.list_user_session(phone.user.user_id)) == 1
    await auth.refresh(phone.refresh_token)


async def test_logout_twice_is_fine(auth: AuthService):
    result = await login(auth)

    await auth.logout(result.refresh_token)
    await auth.logout(result.refresh_token)


@pytest.mark.parametrize("token", [None, "", "garbage", "not-a-uuid.secret"])
async def test_logout_tolerates_missing_or_garbage_tokens(auth: AuthService, token):
    await auth.logout(token)


async def test_logout_with_an_outdated_token_leaves_the_session_alone(
    auth: AuthService, sessions: SessionService
):
    first = await login(auth)
    refreshed = await auth.refresh(first.refresh_token)

    await auth.logout(first.refresh_token)  # stale cookie: not the session's current token

    assert len(await sessions.list_user_session(first.user.user_id)) == 1
    await auth.refresh(refreshed.refresh_token)
