from fastapi import HTTPException

INVALID_SESSION = HTTPException(401, "invalid session")
UNAUTHORIZED = HTTPException(401, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
RESTRICTED = HTTPException(403, "no permission")
