from typing import Annotated

from fastapi import APIRouter
from fastapi_typed_errors import Raises, with_errors

from encosim_server.core import exceptions

router = with_errors(APIRouter())

def func() -> Annotated[None, Raises[exceptions.CharacterOutdated]]:
    raise exceptions.CharacterOutdated()

@router.get("/error")
async def _() -> Annotated[None, Raises[exceptions.CharacterOutdated]]:
    func()
