from http import HTTPStatus
from typing import Literal

from fastapi_typed_errors import BaseError, ErrorResponse

from encosim_server.characters.schema import CharDownload


class InvalidGoogleToken(BaseError[Literal["invalid_google_token"]]):
    http_status = HTTPStatus.UNAUTHORIZED
    description = "Invalid google id token"

class InvalidSession(BaseError[Literal["invalid_session"]]):
    http_status = HTTPStatus.UNAUTHORIZED
    description = "Invalid login session. Please log in again."


class Unauthorized(BaseError[Literal["unauthorized"]]):
    http_status = HTTPStatus.UNAUTHORIZED


class Restricted(BaseError[Literal["access_restricted"]]):
    http_status = HTTPStatus.FORBIDDEN
    description = "Access not permitted"


class Banned(BaseError[Literal["user_banned"]]):
    http_status = HTTPStatus.FORBIDDEN
    description = "User was banned"


class NotFound(BaseError[Literal["not_found"]]):
    http_status = HTTPStatus.NOT_FOUND
    description = "Requested resource does not exist"


class CharacterOutdatedResponse(ErrorResponse):
    correct_version: CharDownload


class CharacterOutdated(BaseError[Literal["character_outdated"]]):
    response_base = CharacterOutdatedResponse
    http_status = HTTPStatus.CONFLICT
    description = "Server already has a character with a newer version"

    def __init__(
        self,
        # a generic subclass would be nice, but the meta class does not handle that well
        attachment: CharDownload,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ):
        self.attachment = attachment
        super().__init__(detail=detail, headers=headers)

    def to_response(self):
        return self.model(
            code=self.error_code,
            detail=str(self.detail),
            correct_version=self.attachment,
        )


class TooManyCharacters(BaseError[Literal["too_many_characters"]]):
    http_status = HTTPStatus.FORBIDDEN
    description = "User already has maximum number of characters"
