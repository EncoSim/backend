from fastapi import HTTPException

INVALID_SESSION = HTTPException(401, "invalid session")
