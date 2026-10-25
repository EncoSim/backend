from typing import Annotated

from fastapi import APIRouter
from fastapi_typed_errors import Raises, with_errors

from encosim_server.core import exceptions
from encosim_server.users.schemas import BanStatus

router = with_errors(APIRouter())


@router.get("/error")
async def _() -> Annotated[None, Raises[exceptions.Banned]]:
    raise exceptions.Banned(attachment=BanStatus(banned=True))
