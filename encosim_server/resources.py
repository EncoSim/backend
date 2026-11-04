from dataclasses import dataclass
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.google import GoogleVerifier
from encosim_server.auth.manager import AuthManager
from encosim_server.auth.sessions import SessionsManager
from encosim_server.celery import PatchedCelery, create_celery
from encosim_server.characters.manager import CharacterManager
from encosim_server.core.settings import Settings
from encosim_server.simulation.manager import SimulationManager
from encosim_server.users.manager import UserManager


@dataclass
class Resources:
    settings: Settings
    google: GoogleVerifier
    db: AsyncDatabase[Any]
    sessions: SessionsManager
    users: UserManager
    auth: AuthManager
    characters: CharacterManager
    celery: PatchedCelery
    simulations: SimulationManager


def build_resources(settings: Settings, google: GoogleVerifier, db: AsyncDatabase[Any]):
    sessions = SessionsManager(settings, db)
    users = UserManager(settings, db, sessions)
    auth = AuthManager(settings, google, sessions, users)
    characters = CharacterManager(settings, db)
    celery = create_celery(settings)
    simulations = SimulationManager(settings, db, celery)
    return Resources(settings, google, db, sessions, users, auth, characters, celery, simulations)
