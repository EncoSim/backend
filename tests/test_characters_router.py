"""Endpoint tests for the characters router (/chars/private)."""

from typing import Any, Literal, overload
from uuid import uuid1, uuid4

import pytest
from encosim.core.entity_config import create_default
from httpx import AsyncClient

from encosim_server.characters.schema import CharDownload, CharUpload
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources
from tests.conftest import LoggedIn, LoginAs

BASE = "/chars/private"


def char_body(hp: int = 50, version: int = 0) -> dict[str, Any]:
    """create character payload"""
    data = create_default().model_dump(mode="json")
    data["health"]["hit_points"] = hp
    return {"version": version, "data": data}


@overload
async def create(user: LoggedIn, hp: int = ..., raw: Literal[False] = ...) -> CharDownload: ...
@overload
async def create(user: LoggedIn, hp: int = ..., raw: Literal[True] = ...) -> dict[str, Any]: ...


async def create(user: LoggedIn, hp: int = 50, raw: bool = False):
    r = await user.client.post(BASE, json=char_body(hp))
    assert r.status_code == 201, r.text
    if raw:
        return r.json()
    return CharDownload.model_validate(r.json(), extra="forbid")


# --- access and validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", BASE, None),
        ("GET", f"{BASE}/{uuid4()}", None),
        ("POST", BASE, char_body()),
        ("PUT", f"{BASE}/{uuid4()}", char_body()),
        ("DELETE", f"{BASE}/{uuid4()}", None),
    ],
)
async def test_anonymous_requests_are_rejected(
    client: AsyncClient, method: str, path: str, body: Any
):
    r = await client.request(method, path, json=body)

    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


@pytest.mark.parametrize(
    "body",
    [{}, {"version": 0}, {"data": char_body()["data"]}, {**char_body(), "version": -1}],
    ids=["empty", "no-data", "no-version", "negative-version"],
)
async def test_malformed_character_is_a_validation_error(login_as: LoginAs, body: Any):
    alice = await login_as()

    assert (await alice.client.post(BASE, json=body)).status_code == 422


@pytest.mark.parametrize("char_id", ["not-a-uuid", str(uuid1())], ids=["garbage", "uuid1"])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_character_ids_must_be_uuid4(login_as: LoginAs, method: str, char_id: str):
    alice = await login_as()

    r = await alice.client.request(
        method, f"{BASE}/{char_id}", json=char_body() if method == "PUT" else None
    )

    assert r.status_code == 422


# --- create, list, get ----------------------------------------------------------------------------


async def test_new_user_has_no_characters(login_as: LoginAs):
    alice = await login_as()

    r = await alice.client.get(BASE)

    assert r.status_code == 200
    assert r.json() == {"characters": []}


async def test_create_returns_the_stored_character(login_as: LoginAs):
    alice = await login_as()

    r = await alice.client.post(BASE, json=char_body(hp=42))

    assert r.status_code == 201
    char = r.json()
    assert char["version"] == 0
    assert char["data"] == char_body(hp=42)["data"]
    assert char["char_id"] and char["created_at"] == char["modified_at"]


async def test_create_ignores_the_version_sent_by_the_client(login_as: LoginAs):
    alice = await login_as()

    r = await alice.client.post(BASE, json=char_body(version=7))

    assert r.json()["version"] == 0


async def test_created_character_can_be_listed_and_fetched(login_as: LoginAs):
    alice = await login_as()
    a, b = await create(alice, hp=10, raw=True), await create(alice, hp=20, raw=True)

    listed = (await alice.client.get(BASE)).json()["characters"]
    fetched = await alice.client.get(f"{BASE}/{a['char_id']}")

    assert {c["char_id"] for c in listed} == {a["char_id"], b["char_id"]}
    assert fetched.status_code == 200
    assert fetched.json() == a


async def test_characters_are_private_to_their_owner(login_as: LoginAs):
    alice, bob = await login_as("google:alice"), await login_as("google:bob")
    char = await create(alice, raw=True)

    assert (await bob.client.get(BASE)).json() == {"characters": []}
    for method in ("GET", "PUT", "DELETE"):
        r = await bob.client.request(
            method, f"{BASE}/{char['char_id']}", json=char_body() if method == "PUT" else None
        )
        assert r.status_code == 404, method
        assert r.json()["code"] == "not_found"
    # ... and nothing happened to it
    assert (await alice.client.get(f"{BASE}/{char['char_id']}")).json() == char


async def test_unknown_character_is_not_found(login_as: LoginAs):
    alice = await login_as()

    r = await alice.client.get(f"{BASE}/{uuid4()}")

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# --- update ---------------------------------------------------------------------------------------


async def test_update_with_the_current_version_stores_it_and_bumps_the_version(login_as: LoginAs):
    alice = await login_as()
    char = await create(alice, hp=10)

    r = await alice.client.put(f"{BASE}/{char.char_id}", json=char_body(hp=99, version=0))

    assert r.status_code == 200
    updated_raw = r.json()
    updated = CharDownload.model_validate(updated_raw, extra="forbid")
    assert updated.version == 1
    assert updated.data.health.hit_points == 99
    assert updated.created_at == char.created_at
    assert updated.modified_at >= char.modified_at
    assert (await alice.client.get(f"{BASE}/{char.char_id}")).json() == updated_raw


async def test_update_with_a_stale_version_returns_the_current_character(login_as: LoginAs):
    alice = await login_as()
    char = await create(alice, hp=10)
    r = await alice.client.put(f"{BASE}/{char.char_id}", json=char_body(hp=20, version=0))

    assert r.status_code == 200
    current = r.json()

    r = await alice.client.put(f"{BASE}/{char.char_id}", json=char_body(hp=30, version=0))

    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "character_outdated"
    assert body["detail"] == current  # lets the client merge or retry
    assert (await alice.client.get(f"{BASE}/{char.char_id}")).json() == current


async def test_update_of_an_unknown_character_is_not_found(login_as: LoginAs):
    alice = await login_as()

    r = await alice.client.put(f"{BASE}/{uuid4()}", json=char_body())

    assert r.status_code == 404


# --- delete ---------------------------------------------------------------------------------------


async def test_delete_returns_the_data_and_removes_the_character(login_as: LoginAs):
    alice = await login_as()
    char = await create(alice, hp=77, raw=True)

    r = await alice.client.delete(f"{BASE}/{char['char_id']}")

    assert r.status_code == 200
    assert r.json() == {"version": 0, "data": char["data"]}  # a payload that re-creates it
    assert (await alice.client.get(f"{BASE}/{char['char_id']}")).status_code == 404
    assert (await alice.client.delete(f"{BASE}/{char['char_id']}")).status_code == 404
    assert (await alice.client.get(BASE)).json() == {"characters": []}


async def test_deleted_character_can_be_uploaded_again(login_as: LoginAs):
    alice = await login_as()
    char = await create(alice, hp=77)
    undo = (await alice.client.delete(f"{BASE}/{char.char_id}")).json()

    r = await alice.client.post(BASE, json=undo)

    assert r.status_code == 201
    assert r.json()["data"] == char.data.model_dump(mode="json")
    assert r.json()["char_id"] != char.char_id  # it is a new character


# --- limit ----------------------------------------------------------------------------------------


async def test_character_limit_is_enforced(
    login_as: LoginAs,
    resources: Resources,
    settings: Settings,
):
    alice = await login_as()
    payload = CharUpload.model_validate(char_body())
    for _ in range(settings.max_characters):
        await resources.characters.create(alice.user.user_id, payload)

    r = await alice.client.post(BASE, json=char_body())

    assert r.status_code == 403
    assert r.json()["code"] == "too_many_characters"
    assert r.json()["detail"] == {"allowed": settings.max_characters}
