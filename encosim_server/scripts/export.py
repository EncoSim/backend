import json
import sys

from encosim import EntityConfig
from encosim.core.entity_config import create_default

from encosim_server.app import create_app


def default_character():
    default: EntityConfig = create_default()
    print(default.model_dump_json(indent=2))


def openapi_json():
    app = create_app()
    schema = app.openapi()
    json.dump(schema, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
