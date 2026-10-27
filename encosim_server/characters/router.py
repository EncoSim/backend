from fastapi import APIRouter
from pydantic import UUID4

from encosim_server.characters.schema import CharDownload, CharUpload, ManyCharDownload
from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep

router = APIRouter(prefix="/chars/private", tags=["characters"])


@router.get("")
async def list_characters(res: ResourcesDep, user: ActiveUserDep) -> ManyCharDownload:
    return ManyCharDownload(characters=await res.characters.list(user.user_id))

COMMON = (errors.NotFound, errors.Unauthorized)

@router.get("/{char_id}", responses=errors.to_responses(*COMMON))
async def get_character(res: ResourcesDep, user: ActiveUserDep, char_id: UUID4) -> CharDownload:
    return await res.characters.get(char_id, user.user_id)


@router.post("", status_code=201, responses=errors.to_responses(*COMMON, errors.TooManyCharacters))
async def create_character(
    res: ResourcesDep, user: ActiveUserDep, payload: CharUpload
) -> CharDownload:
    # TODO: add priviledges
    return await res.characters.create(user.user_id, payload)


@router.put("/{char_id}", responses=errors.to_responses(*COMMON, errors.CharacterOutdated))
async def update_character(
    res: ResourcesDep, user: ActiveUserDep, char_id: UUID4, payload: CharUpload
) -> CharDownload:
    return await res.characters.update(char_id=char_id, owner_id=user.user_id, payload=payload)


@router.delete("/{char_id}", responses=errors.to_responses(*COMMON))
async def delete_character(res: ResourcesDep, user: ActiveUserDep, char_id: UUID4) -> CharUpload:
    char = await res.characters.delete(char_id=char_id, owner_id=user.user_id)
    return CharUpload(version=0, data=char.data)
