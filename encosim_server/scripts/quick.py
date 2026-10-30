from encosim_server.core.settings import Settings, get_settings
from encosim_server.database import make_mongo_uri

print(make_mongo_uri(get_settings()))
