from encosim import EntityConfig
from encosim.core.entity_config import create_default


def default_character():
    default: EntityConfig = create_default()
    print(default.model_dump_json(indent=2))
