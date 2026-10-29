"""Endpoint tests for the users router (/user).

Two of these are strict xfails: they describe the intended behavior of code that currently has
a bug (see the `reason`). Remove the marker once the bug is fixed; until then the suite
turns red if the bug disappears without the marker being removed.

DRAFT: not run yet.
"""

from typing import Any

import pytest
from encosim.core.entity_config import create_default
from httpx import AsyncClient
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.characters.schema import CharUpload
from encosim_server.database import CHARACTERS, SESSIONS
from encosim_server.resources import Resources
from tests.conftest import LoginAs

PROFILE = "/user/profile"


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_profile_requires_login(client: AsyncClient, method: str):
    r = await client.request(method, PROFILE)

    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


async def test_profile_returns_the_google_profile(login_as: LoginAs):
    alice = await login_as("google:alice")

    r = await alice.client.get(PROFILE)

    assert r.status_code == 200
    assert r.json() == {
        "name": "alice",
        "email": "alice@example.com",
        "email_verified": True,
        "picture": None,
    }


async def test_profile_belongs_to_the_logged_in_user(login_as: LoginAs):
    alice, bob = await login_as("google:alice"), await login_as("google:bob")

    assert (await alice.client.get(PROFILE)).json()["email"] == "alice@example.com"
    assert (await bob.client.get(PROFILE)).json()["email"] == "bob@example.com"


async def test_profile_of_a_deleted_user_is_rejected(login_as: LoginAs, resources: Resources):
    alice = await login_as()
    await resources.users.delete(alice.user.user_id)

    r = await alice.client.get(PROFILE)

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_session"


async def test_profile_requires_a_live_session(login_as: LoginAs, resources: Resources):
    alice = await login_as()
    await resources.sessions.terminate(alice.session_id)  # e.g. logged out on another device

    r = await alice.client.get(PROFILE)  # the access token itself is still valid

    assert r.status_code == 401
    assert r.json()["code"] == "invalid_session"


async def test_delete_profile_removes_user_sessions_and_characters(
    login_as: LoginAs, resources: Resources, db: AsyncDatabase[Any]
):
    alice, bob = await login_as("google:alice"), await login_as("google:bob")
    payload = CharUpload(version=0, data=create_default())
    await resources.characters.create(alice.user.user_id, payload)
    await resources.characters.create(bob.user.user_id, payload)

    r = await alice.client.delete(PROFILE)

    assert r.status_code == 200
    assert await resources.users.get(alice.user.user_id) is None
    assert await db[SESSIONS].count_documents({"user_id": alice.user.user_id}) == 0
    assert await db[CHARACTERS].count_documents({"owner_id": alice.user.user_id}) == 0
    # other users keep everything
    assert await resources.users.get(bob.user.user_id) is not None
    assert await db[CHARACTERS].count_documents({"owner_id": bob.user.user_id}) == 1
    assert (await bob.client.get(PROFILE)).status_code == 200


async def test_deleted_user_can_no_longer_use_the_api(login_as: LoginAs, resources: Resources):
    """Independent of delete_profile: whatever deletes the user must lock their token out."""
    alice = await login_as()
    await resources.users.delete(alice.user.user_id)

    assert (await alice.client.get(PROFILE)).status_code == 401
    assert (await alice.client.get("/auth/sessions")).status_code == 401
