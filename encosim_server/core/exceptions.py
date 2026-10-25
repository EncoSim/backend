from http import HTTPStatus
from typing import Literal

from fastapi_typed_errors import BaseError, ErrorResponse
from pydantic.main import BaseModel

from encosim_server.characters.schema import CharDownload
from encosim_server.users.schemas import BanStatus


class BaseErrorWithAttachment[T: str](BaseError[T]):
    def __init__(
        self,
        # explicit type annotation would be nice, but the meta class does not handle those generics
        attachment: BaseModel,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ):
        self.attachment = attachment
        super().__init__(detail=detail, headers=headers)

    def to_response(self):
        return self.model(code=self.error_code, detail=str(self.detail), attachment=self.attachment)


class InvalidSession(BaseError[Literal["invalid_session"]]):
    http_status = HTTPStatus.UNAUTHORIZED
    description = "Invalid login session. Please log in again."


class Unauthorized(BaseError[Literal["unauthorized"]]):
    http_status = HTTPStatus.UNAUTHORIZED


class Restricted(BaseError[Literal["no_permission"]]):
    http_status = HTTPStatus.FORBIDDEN
    description = "Access not permitted"


class _Banned[T: str](ErrorResponse[T]):
    attachment: BanStatus


class Banned(BaseErrorWithAttachment[Literal["banned"]]):
    http_status = HTTPStatus.FORBIDDEN
    description = "banned"
    response_base = _Banned


class _Outdated(ErrorResponse):
    attachment: CharDownload


class Outdated(BaseError[Literal["version_conflict"]]):
    http_status = HTTPStatus.CONFLICT
    description = "Server has a newer version"
    response_base = _Outdated
