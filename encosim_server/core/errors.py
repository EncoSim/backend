import logging
from typing import Annotated, Any, ClassVar, Literal, Mapping, Self, Union

from fastapi import Request
from pydantic import BaseModel, Field, create_model
from starlette.responses import JSONResponse

from encosim_server.characters.schema import CharDownload

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type["ApiError"]] = {}


class ApiError(Exception):
    status_code: ClassVar[int]
    code: ClassVar[str]
    default_message: ClassVar[str]
    detail_model: ClassVar[type[BaseModel] | None] = None
    detail_description: ClassVar[str] = "error-specific payload"
    default_headers: ClassVar[dict[str, str]] = {}

    # generated
    body_model: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs):
        super().__init_subclass__(**kwargs)
        if abstract:
            return
        # TODO: linter/type hint support would be nice
        for attr in ("code", "status_code", "default_message"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"{cls.__name__} must define `{attr}` (or be declared `abstract=True`)"
                )
        if cls.detail_model is not None and not (
            isinstance(cls.detail_model, type) and issubclass(cls.detail_model, BaseModel)
        ):
            raise TypeError(f"{cls.__name__}.detail_model must be a pydantic model")

        existing = _REGISTRY.get(cls.code)
        if existing is not None and (existing.__module__, existing.__qualname__) != (
            cls.__module__,
            cls.__qualname__,
        ):
            raise TypeError(
                f"Error code {cls.code} already registered for {existing.__qualname__}."
                "Only one subclass per error code!"
            )
        _REGISTRY[cls.code] = cls
        cls.body_model = cls._build_body_model()

    @classmethod
    def _build_body_model(cls):
        fields: dict[str, Any] = {
            "code": (
                Literal[cls.code],  # type: ignore - not an enum member, still possible
                Field(description="machine-readable error code"),
            ),
            "message": (str, Field(description="human-readable error description")),
        }
        if cls.detail_model is not None:
            fields["detail"] = (cls.detail_model, Field(description=cls.detail_description))
        return create_model(f"{cls.__name__}Body", __module__=cls.__module__, **fields)

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        name = type(self).__name__
        if self.detail_model is not None:
            if detail is None:
                raise TypeError(f"{name} requires detail= ({self.detail_model.__name__})")
            detail = self.detail_model.model_validate(detail, from_attributes=True)
        elif detail is not None:
            raise TypeError(f"{name} does not take a detail")
        self.message = message or self.default_message
        self.detail = detail
        self.headers = {**self.default_headers, **(headers or {})}
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        content: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            content["detail"] = self.detail.model_dump(mode="json")
        return content


def handle_api_error(_: Request, exc: ApiError):
    logger.debug("%s", exc_info=exc)
    if exc.status_code >= 500:
        logger.error("%s: %s", exc.code, exc.message)
    return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)


class Raises:
    """Document which `ApiError`s are raised by a function"""

    __slots__ = ("errors",)

    errors: tuple[type[ApiError], ...]

    def __init__(self, *errors: type[ApiError]):
        for e in errors:
            if not isinstance(e, type) and issubclass(e, Exception):
                raise TypeError("{e} is not an valid `Exception`")
        self.errors = errors

    def __class_getitem__(cls, errors: type[ApiError] | tuple[type[ApiError], ...]) -> Self:
        items = errors if isinstance(errors, tuple) else (errors,)
        return cls(*items)

    def __repr__(self) -> str:
        names = ", ".join(error.__name__ for error in self.errors)
        return f"Raises[{names}]"


def to_responses(*classes: type[ApiError]) -> dict[int | str, dict[str, Any]]:
    """Value for `responses=` on a route, router or app"""
    by_status: dict[int, list[type[ApiError]]] = {}
    for cls in dict.fromkeys(classes):  # de-duplicate, keep order
        by_status.setdefault(cls.status_code, []).append(cls)

    responses: dict[int | str, dict[str, Any]] = {}
    for status, group in by_status.items():
        models = [c.body_model for c in group]
        model = models[0]
        if len(models) > 1:
            model = Annotated[
                Union[tuple(models)],  # type: ignore - function calls not allowed, but they do work?!
                Field(discriminator="code"),
            ]
        responses[status] = {
            "model": model,
            "description": "; ".join(f"`{c.code}`: {c.default_message}" for c in group),
        }
    return responses


class InvalidGoogleToken(ApiError):
    status_code = 401
    code = "invalid_google_token"
    default_message = "Invalid google id token"


class InvalidSession(ApiError):
    status_code = 401
    code = "invalid_session"
    default_message = "Invalid login session. Please log in again."


class Unauthorized(ApiError):
    status_code = 401
    code = "unauthorized"
    default_message = "Could not validate credentials"
    default_headers = {"WWW-Authenticate": "Bearer"}


class Restricted(ApiError):
    status_code = 403
    code = "access_restricted"
    default_message = "Insufficient permission to access resource"


class Banned(ApiError):
    status_code = 403
    code = "banned"
    default_message = "User was banned"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"
    default_message = "Requested resource does not exist"


class CharacterOutdated(ApiError):
    status_code = 409
    code = "character_outdated"
    default_message = "Server already has a character with a newer version"
    detail_model = CharDownload
    detail_description = "Current version of character"

class AllowedNumberDetail(BaseModel):
    allowed: int

class TooManyCharacters(ApiError):
    status_code = 403
    code = "too_many_characters"
    default_message = "User already has maximum number of characters"
    detail_model = AllowedNumberDetail


class TooManyParticipants(ApiError):
    status_code = 403
    code = "too_many_participants"
    default_message = "Exceeded total number of allowed simulation participants"
    detail_model = AllowedNumberDetail


class TooManyRepetitions(ApiError):
    status_code = 403
    code = "too_many_repetitions"
    default_message = "Exceeded total number of allowed simulation repetitions"
    detail_model = AllowedNumberDetail


class SimulationTimeout(ApiError):
    status_code = 408
    code = "simulation_timeout"
    default_message = "Exceeded total simulation time"


class TooManySimulations(ApiError):
    status_code = 429
    code = "too_many_simulations"
    default_message = "Exceeded total number of allowed simulations"
    detail_model = AllowedNumberDetail
