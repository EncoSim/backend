from fastapi import APIRouter
from fastapi_typed_errors import with_errors
from pydantic import UUID4

from encosim_server.characters.schema import CharDownload, CharUpload, ManyCharDownload
from encosim_server.core import exceptions
from encosim_server.depends import ActiveUserDep, ResourcesDep

router = with_errors(APIRouter(prefix="/chars/private"), auto=True)


@router.get("")
async def list_characters(res: ResourcesDep, user: ActiveUserDep) -> ManyCharDownload:
    return ManyCharDownload(characters=await res.characters.list(user.user_id))


@router.get("/{char_id}")
async def get_character(res: ResourcesDep, user: ActiveUserDep, char_id: UUID4) -> CharDownload:
    return await res.characters.get(char_id, user.user_id)


@router.post("")
async def create_character(
    res: ResourcesDep, user: ActiveUserDep, payload: CharUpload
) -> CharDownload:
    # TODO: add priviledges
    async with res.db.client.start_session() as session:
        n = await res.characters.count_owned(user.user_id)
        if n >= res.settings.max_characters:
            raise exceptions.TooManyCharacters()
        return await res.characters.create(user.user_id, payload)
