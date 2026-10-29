
import pytest

from encosim_server.resources import Resources


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
