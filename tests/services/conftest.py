from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from encosim_server.auth.google import FakeGoogleVerifier
from encosim_server.core.settings import Settings
from encosim_server.resources import Resources, build_resources


@pytest.fixture
def users(resources: Resources):
    return resources.users


@pytest.fixture
def sessions(resources: Resources):
    return resources.sessions


@pytest.fixture
def auth(resources: Resources):
    return resources.auth


@pytest.fixture
def characters(resources: Resources):
    return resources.characters
