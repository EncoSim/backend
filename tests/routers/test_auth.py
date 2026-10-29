"""Endpoint tests for the auth router (/auth): login, refresh cookie, logout and sessions.

These go through HTTP with a real cookie jar. The Google check is the `FakeGoogleVerifier`
(token format `fake:<sub>:<email>`). The test client talks to `https://localhost/api`, like the
app's `root_path`, so the refresh cookie gets the path the browser would see.

DRAFT: not run yet.
"""

from uuid import UUID, uuid4

import httpx
import pytest
from httpx import AsyncClient

from encosim_server.core.security import decode_access_token
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources
from tests.conftest import MakeClient
from tests.helpers import CHROME_WIN, FIREFOX_LINUX, fake_token

COOKIE_PATH = "/api/auth"


async def login(
    client: httpx.AsyncClient, sub: str = "alice", user_agent: str = CHROME_WIN
) -> httpx.Response:
    """login to router endpoint with fake google id"""
    r = await client.post(
        "/auth/login/google",
        json={"id_token": fake_token(sub)},
        headers={"User-Agent": user_agent},
    )
    assert r.status_code in (200, 201), r.text
    return r


def bearer(login_response: httpx.Response) -> dict[str, str]:
    return {"Authorization": f"Bearer {login_response.json()['access_token']}"}


def other_client_with_cookie(
    make_client: MakeClient, settings: Settings, token: str
) -> httpx.AsyncClient:
    """A second browser that holds the given refresh token."""
    other = make_client()
    other.cookies.set(settings.cookie_name, token, path=COOKIE_PATH)
    return other


# --- login ----------------------------------------------------------------------------------------


async def test_first_login_creates_the_user_and_sets_the_refresh_cookie(
    client: AsyncClient, settings: Settings
):
    r = await login(client)

    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"user_id", "profile", "access_token"}
    assert body["profile"]["email"] == "alice@example.com"
    assert body["profile"]["email_verified"] is True
    assert decode_access_token(settings, body["access_token"]).sub == UUID(body["user_id"])

    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith(f"{settings.cookie_name}=")
    assert "HttpOnly" in set_cookie
    assert f"Path={COOKIE_PATH}" in set_cookie  # the path the browser sees, not just /auth
    assert client.cookies.get(settings.cookie_name)


async def test_second_login_returns_the_same_user_with_status_200(
    client: AsyncClient,
):
    first = await login(client)
    second = await login(client)

    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.status_code == 201
    assert second.status_code == 200


async def test_different_google_accounts_are_different_users(
    make_client: MakeClient,
):
    alice = await login(make_client(), "alice")
    bob = await login(make_client(), "bob")

    assert alice.json()["user_id"] != bob.json()["user_id"]


@pytest.mark.parametrize("token", ["", "garbage", "fake:only-two", "real:alice:alice@example.com"])
async def test_invalid_google_token_is_rejected(
    client: AsyncClient, settings: Settings, token: str
):
    r = await client.post("/auth/login/google", json={"id_token": token})

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_google_token"
    assert client.cookies.get(settings.cookie_name) is None


async def test_login_without_a_token_is_a_validation_error(client: AsyncClient):
    assert (await client.post("/auth/login/google", json={})).status_code == 422


async def test_banned_user_cannot_log_in(make_client: MakeClient, resources: Resources):
    first = await login(make_client())
    await resources.users.ban(UUID(first.json()["user_id"]), "spam")

    r = await make_client().post("/auth/login/google", json={"id_token": fake_token()})

    assert r.status_code == 403
    assert r.json()["code"] == "banned"


# --- refresh --------------------------------------------------------------------------------------


async def test_refresh_rotates_the_cookie_and_returns_a_new_access_token(
    client: AsyncClient, settings: Settings
):
    first = await login(client)
    old_cookie = client.cookies.get(settings.cookie_name)

    r = await client.post("/auth/refresh")

    assert r.status_code == 200
    assert r.json()["user_id"] == first.json()["user_id"]
    assert decode_access_token(settings, r.json()["access_token"]).sub == UUID(
        first.json()["user_id"]
    )
    new_cookie = client.cookies.get(settings.cookie_name)
    assert new_cookie and new_cookie != old_cookie
    # the new access token belongs to the same session: it is still listed
    sessions = await client.get("/auth/sessions", headers=bearer(r))
    assert len(sessions.json()["sessions"]) == 1


async def test_refresh_can_be_repeated(client: AsyncClient):
    await login(client)
    for _ in range(3):
        assert (await client.post("/auth/refresh")).status_code == 200


async def test_refresh_without_a_cookie_is_rejected(client: AsyncClient):
    r = await client.post("/auth/refresh")

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_session"


async def test_refresh_with_a_garbage_cookie_is_rejected(client: AsyncClient, settings: Settings):
    client.cookies.set(settings.cookie_name, "garbage", domain="localhost", path=COOKIE_PATH)

    r = await client.post("/auth/refresh")

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_session"


async def test_replaying_an_old_refresh_cookie_kills_the_session(
    make_client: MakeClient, settings: Settings
):
    victim = make_client()
    await login(victim)
    stolen = victim.cookies.get(settings.cookie_name)
    assert stolen
    await victim.post("/auth/refresh")  # the legitimate user moves on to a new token

    thief = other_client_with_cookie(make_client, settings, stolen)
    r = await thief.post("/auth/refresh")

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_session"
    # the replay is treated as theft: the legitimate token is dead as well
    assert (await victim.post("/auth/refresh")).status_code == 401


async def test_banned_user_cannot_refresh(client: AsyncClient, resources: Resources):
    first = await login(client)
    await resources.users.ban(UUID(first.json()["user_id"]), "spam")

    assert (await client.post("/auth/refresh")).status_code == 401


# --- logout ---------------------------------------------------------------------------------------


async def test_logout_ends_the_session_and_clears_the_cookie(
    make_client: MakeClient, settings: Settings
):
    client = make_client()
    first = await login(client)
    token = client.cookies.get(settings.cookie_name)
    assert token

    r = await client.post("/auth/logout")

    assert r.status_code == 200
    assert client.cookies.get(settings.cookie_name) is None
    # the session is really gone: neither the refresh token nor the access token work any more
    thief = other_client_with_cookie(make_client, settings, token)
    assert (await thief.post("/auth/refresh")).status_code == 401
    assert (await client.get("/auth/sessions", headers=bearer(first))).status_code == 401


@pytest.mark.parametrize("cookie", [None, "garbage"])
async def test_logout_is_silent_without_a_valid_session(
    client: AsyncClient, settings: Settings, cookie: str | None
):
    if cookie:
        client.cookies.set(settings.cookie_name, cookie, domain="localhost", path=COOKIE_PATH)

    assert (await client.post("/auth/logout")).status_code == 200


# --- sessions -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/sessions"),
        ("DELETE", "/auth/sessions"),
        ("DELETE", f"/auth/sessions/{uuid4()}"),
    ],
)
async def test_session_endpoints_require_login(client: AsyncClient, method: str, path: str):
    r = await client.request(method, path)

    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


async def test_sessions_lists_every_login_without_secrets(make_client: MakeClient):
    chrome, firefox = make_client(), make_client()
    await login(chrome, user_agent=CHROME_WIN)
    second = await login(firefox, user_agent=FIREFOX_LINUX)

    r = await firefox.get("/auth/sessions", headers=bearer(second))

    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 2
    assert any("Chrome" in s["info"] for s in sessions)
    assert any("Firefox" in s["info"] for s in sessions)
    assert all(set(s) == {"session_id", "info", "last_used_at", "expires_at"} for s in sessions)


async def test_sessions_only_lists_the_users_own(make_client: MakeClient):
    alice = make_client()
    alice_login = await login(alice, "alice")
    await login(make_client(), "bob")

    r = await alice.get("/auth/sessions", headers=bearer(alice_login))

    assert len(r.json()["sessions"]) == 1


async def test_terminate_one_session_keeps_the_others(make_client: MakeClient, settings: Settings):
    chrome, firefox = make_client(), make_client()
    first = await login(chrome, user_agent=CHROME_WIN)
    await login(firefox, user_agent=FIREFOX_LINUX)
    listed = (await chrome.get("/auth/sessions", headers=bearer(first))).json()["sessions"]
    firefox_id = next(s["session_id"] for s in listed if "Firefox" in s["info"])
    firefox_token = firefox.cookies.get(settings.cookie_name)
    assert firefox_token

    r = await chrome.delete(f"/auth/sessions/{firefox_id}", headers=bearer(first))

    assert r.status_code == 200
    remaining = (await chrome.get("/auth/sessions", headers=bearer(first))).json()["sessions"]
    assert len(remaining) == 1
    assert remaining[0]["session_id"] != firefox_id
    assert "Chrome" in remaining[0]["info"]
    assert (await firefox.post("/auth/refresh")).status_code == 401  # that browser is logged out


async def test_terminate_unknown_session_is_not_found(client: AsyncClient):
    first = await login(client)

    r = await client.delete(f"/auth/sessions/{uuid4()}", headers=bearer(first))

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


async def test_terminate_session_of_another_user_is_not_found(
    make_client: MakeClient,
):
    alice, bob = make_client(), make_client()
    alice_login = await login(alice, "alice")
    bob_login = await login(bob, "bob")
    bobs_session = (await bob.get("/auth/sessions", headers=bearer(bob_login))).json()["sessions"][
        0
    ]["session_id"]

    r = await alice.delete(f"/auth/sessions/{bobs_session}", headers=bearer(alice_login))

    assert r.status_code == 404
    assert len((await bob.get("/auth/sessions", headers=bearer(bob_login))).json()["sessions"]) == 1


async def test_terminate_session_with_a_malformed_id_is_a_validation_error(client: AsyncClient):
    first = await login(client)

    r = await client.delete("/auth/sessions/not-a-uuid", headers=bearer(first))

    assert r.status_code == 422


async def test_terminate_all_sessions_logs_the_user_out_everywhere(
    make_client: MakeClient,
):
    chrome, firefox, bob = make_client(), make_client(), make_client()
    first = await login(chrome, "alice", CHROME_WIN)
    await login(firefox, "alice", FIREFOX_LINUX)
    bob_login = await login(bob, "bob")

    r = await chrome.delete("/auth/sessions", headers=bearer(first))

    assert r.status_code == 200
    assert (await chrome.post("/auth/refresh")).status_code == 401
    assert (await firefox.post("/auth/refresh")).status_code == 401
    # even the access token used for the request is dead now (sessions are checked again)
    assert (await chrome.get("/auth/sessions", headers=bearer(first))).status_code == 401
    # other users are not affected
    assert (await bob.get("/auth/sessions", headers=bearer(bob_login))).status_code == 200
