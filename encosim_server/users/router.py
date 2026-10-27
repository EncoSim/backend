from fastapi import APIRouter

from encosim_server.core import errors
from encosim_server.depends import ActiveUserDep, ResourcesDep
from encosim_server.users.schemas import UserProfile

router = APIRouter(
    prefix="user",
    tags=["user"],
    responses=errors.to_responses(errors.Unauthorized, errors.InvalidSession),
)


@router.get("/profile")
async def profile(res: ResourcesDep, user: ActiveUserDep) -> UserProfile:
    doc = await res.users.get(user.user_id)
    if doc is None or not res.sessions.is_valid(user.user_id, user.session_id):
        raise errors.InvalidSession()
    return doc.profile


@router.delete("/profile", description="Hard-delete the user, their sessions and their characters.")
async def delete_profile(res: ResourcesDep, user: ActiveUserDep):
    if not res.session.is_valid(user.user_id, user.session_id):
        raise errors.InvalidSession()
    await res.users.delete(user.user_id)
