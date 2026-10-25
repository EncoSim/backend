"""Tests for CharacterService (DRAFT, not run).

Needs `clock` to patch `utcnow` in encosim_server.characters.service as well
(see the notes in the chat message).

ADAPT: `upload()` builds a CharUpload. I only know that the schema has `data` and `version`
(from service.py); adjust it if `data` is not a plain dict or if more fields are required.
"""

import asyncio
import json
import logging
from uuid import UUID, uuid4

import pytest
from encosim.core.entity_config import create_default
from fastapi import HTTPException
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.characters.schema import CharUpload
from encosim_server.characters.service import CharacterService
from encosim_server.database import CHARACTERS


def create_payload(hp: int = 50, version: int = 0) -> CharUpload:
    payload = CharUpload(data=create_default(), version=version)
    payload.data.health.hit_points = hp
    return payload


@pytest.fixture
def alice():
    return uuid4()


@pytest.fixture
def bob():
    return uuid4()


async def raw(db: AsyncDatabase, char_id: UUID):
    return await db[CHARACTERS].find_one({"_id": char_id})


# --- create ------------------------------------------------------------------------------


async def test_create_sets_initial_state_and_persists(
    characters: CharacterService, db: AsyncDatabase, alice, clock
):
    char = await characters.create(alice, create_payload())

    assert char.owner_id == alice
    assert char.version == 0
    assert char.created_at == clock() and char.modified_at == clock()
    assert char.data == (await characters.get(char.char_id, alice)).data
    assert await db[CHARACTERS].count_documents({"_id": char.char_id, "owner_id": alice}) == 1


async def test_create_ignores_a_client_supplied_version(characters: CharacterService, alice):
    char = await characters.create(alice, create_payload(version=5))
    assert char.version == 0


async def test_create_assigns_distinct_ids(characters: CharacterService, alice):
    a = await characters.create(alice, create_payload())
    b = await characters.create(alice, create_payload())
    assert a.char_id != b.char_id


# --- get -----------------------------------------------------------------------------------


async def test_get_returns_the_owners_character(characters: CharacterService, alice):
    created = await characters.create(alice, create_payload())

    fetched = await characters.get(created.char_id, alice)

    assert fetched.char_id == created.char_id
    assert fetched.owner_id == alice
    assert fetched.version == created.version
    assert fetched.data == created.data


async def test_get_unknown_character_is_404(characters: CharacterService, alice):
    with pytest.raises(HTTPException) as exc:
        await characters.get(uuid4(), alice)
    assert exc.value.status_code == 404


async def test_get_someone_elses_character_is_404(characters: CharacterService, alice, bob):
    char = await characters.create(alice, create_payload())
    with pytest.raises(HTTPException) as exc:
        await characters.get(char.char_id, bob)
    assert exc.value.status_code == 404


# --- list / count ------------------------------------------------------------------------------


async def test_list_returns_only_the_owners_characters(characters: CharacterService, alice, bob):
    mine = set()
    for _ in range(2):
        mine.add((await characters.create(alice, create_payload())).char_id)
    await characters.create(bob, create_payload())

    listed = await characters.list(alice)

    assert {c.char_id for c in listed} == mine


async def test_list_without_characters_is_empty(characters: CharacterService, alice):
    assert await characters.list(alice) == []


async def test_list_skips_documents_that_fail_validation(
    characters: CharacterService, db: AsyncDatabase, alice, caplog
):
    caplog.set_level(logging.DEBUG, logger="encosim_server")
    good = await characters.create(alice, create_payload())
    bad_id = uuid4()
    await db[CHARACTERS].insert_one({"_id": bad_id, "owner_id": alice, "unexpected": "shape"})

    listed = await characters.list(alice)

    assert [c.char_id for c in listed] == [good.char_id]
    assert str(bad_id) in caplog.text  # skipped, but not silently


async def test_count_owned_counts_only_the_owners_characters(
    characters: CharacterService, alice, bob
):
    assert await characters.count_owned(alice) == 0
    await characters.create(alice, create_payload())
    await characters.create(alice, create_payload())
    await characters.create(bob, create_payload())

    assert await characters.count_owned(alice) == 2
    assert await characters.count_owned(bob) == 1


# --- update (optimistic concurrency) --------------------------------------------------------------


async def test_update_replaces_data_and_returns_the_new_state(characters: CharacterService, alice):
    char = await characters.create(alice, create_payload(hp=1))

    updated = await characters.update(
        char.char_id, alice, create_payload(hp=2, version=char.version)
    )

    assert updated.version == char.version + 1
    assert updated == await characters.get(char.char_id, alice)


async def test_update_keeps_immutable_fields_and_bumps_modified_at(
    characters: CharacterService, alice, clock
):
    char = await characters.create(alice, create_payload(hp=1))
    clock.tick(minutes=5)

    updated = await characters.update(char.char_id, alice, create_payload(hp=2, version=0))

    assert updated.char_id == char.char_id
    assert updated.owner_id == alice
    assert updated.created_at == char.created_at
    assert updated.modified_at == clock()
    assert (await characters.get(char.char_id, alice)).modified_at == clock()


async def test_consecutive_updates_chain_on_the_returned_version(
    characters: CharacterService, alice
):
    char = await characters.create(alice, create_payload(hp=7))

    first = await characters.update(
        char.char_id,
        alice,
        create_payload(hp=2, version=char.version),
    )
    second = await characters.update(
        char.char_id,
        alice,
        create_payload(hp=3, version=first.version),
    )

    assert (char.version, first.version, second.version) == (0, 1, 2)


async def test_update_with_a_stale_version_conflicts_and_changes_nothing(
    characters: CharacterService, alice
):
    char = await characters.create(alice, create_payload(hp=1))
    winner = await characters.update(char.char_id, alice, create_payload(hp=2, version=0))

    with pytest.raises(HTTPException) as exc:
        await characters.update(char.char_id, alice, create_payload(hp=3, version=0))  # stale

    assert exc.value.status_code == 409
    current = await characters.get(char.char_id, alice)
    assert current.version == 1
    assert current.data == winner.data  # the stale write was dropped


async def test_conflict_reports_the_current_server_state(characters: CharacterService, alice):
    char = await characters.create(alice, create_payload(hp=1))
    await characters.update(char.char_id, alice, create_payload(hp=4242, version=0))

    with pytest.raises(HTTPException) as exc:
        await characters.update(char.char_id, alice, create_payload(hp=2, version=0))

    # TODO: fix error reporting with new http exception model
    body = json.dumps(exc.value.detail)  # this is what the HTTP layer does with it
    assert "4242" in body  # the client gets the winning data ...
    assert '"version": 1' in body  # ... and the version to base its next attempt on


async def test_concurrent_updates_with_the_same_version_only_one_wins(
    characters: CharacterService, alice
):
    char = await characters.create(alice, create_payload(hp=1))

    results = await asyncio.gather(
        characters.update(char.char_id, alice, create_payload(hp=2, version=0)),
        characters.update(char.char_id, alice, create_payload(hp=3, version=0)),
        return_exceptions=True,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, HTTPException)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].status_code == 409
    final = await characters.get(char.char_id, alice)
    assert final.version == 1
    assert final.data == winners[0].data


async def test_update_unknown_character_is_404_not_409(characters: CharacterService, alice):
    with pytest.raises(HTTPException) as exc:
        await characters.update(uuid4(), alice, create_payload())
    assert exc.value.status_code == 404


async def test_cannot_update_someone_elses_character(characters: CharacterService, alice, bob):
    char = await characters.create(alice, create_payload(hp=1))

    with pytest.raises(HTTPException) as exc:
        await characters.update(char.char_id, bob, create_payload(hp=6, version=0))

    assert exc.value.status_code == 404  # same answer as for a missing id: no information leak
    untouched = await characters.get(char.char_id, alice)
    assert untouched.version == 0
    assert untouched.data == char.data


# --- delete ---------------------------------------------------------------------------------------


async def test_delete_removes_only_that_character(characters: CharacterService, alice):
    keep = await characters.create(alice, create_payload())
    gone = await characters.create(alice, create_payload())

    await characters.delete(gone.char_id, alice)

    assert [c.char_id for c in await characters.list(alice)] == [keep.char_id]


async def test_delete_unknown_or_already_deleted_character_is_404(
    characters: CharacterService, alice
):
    char = await characters.create(alice, create_payload())
    await characters.delete(char.char_id, alice)

    for char_id in (char.char_id, uuid4()):
        with pytest.raises(HTTPException) as exc:
            await characters.delete(char_id, alice)
        assert exc.value.status_code == 404


async def test_cannot_delete_someone_elses_character(characters: CharacterService, alice, bob):
    char = await characters.create(alice, create_payload())

    with pytest.raises(HTTPException) as exc:
        await characters.delete(char.char_id, bob)

    assert exc.value.status_code == 404
    assert (await characters.get(char.char_id, alice)).char_id == char.char_id
