from dataclasses import dataclass

from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.google import GoogleVerifier
from encosim_server.auth.service import AuthService
from encosim_server.auth.sessions import SessionService
from encosim_server.celery import PatchedCelery, create_celery
from encosim_server.characters.service import CharacterService
from encosim_server.core.settings import Settings
from encosim_server.users.service import UserService


@dataclass
class Resources:
    settings: Settings
    google: GoogleVerifier
    db: AsyncDatabase
    sessions: SessionService
    users: UserService
    auth: AuthService
    characters: CharacterService
    celery: PatchedCelery


def build_resources(settings: Settings, google: GoogleVerifier, db: AsyncDatabase):
    sessions = SessionService(settings, db)
    users = UserService(settings, db, sessions)
    auth = AuthService(settings, google, sessions, users)
    characters = CharacterService(settings, db)
    celery = create_celery(settings)
    return Resources(settings, google, db, sessions, users, auth, characters, celery)
