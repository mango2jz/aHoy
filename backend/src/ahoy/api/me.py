"""`/api/me`: the signed-in user's profile and the private-repo notice (T14 adds settings)."""

from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel

from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB
from ahoy.store import auth as auth_store

router = APIRouter(prefix="/api")


class Me(BaseModel):
    id: UUID
    login: str
    avatar_url: str
    private_repo_notice_ack: bool


@router.get("/me")
async def get_me(user: CurrentUser) -> Me:
    return Me(
        id=user.id,
        login=user.github_login,
        avatar_url=user.avatar_url,
        private_repo_notice_ack=user.private_repo_notice_ack_at is not None,
    )


@router.post("/me/private-repo-notice", status_code=204)
async def ack_private_repo_notice(user: CurrentUser, db: DB) -> Response:
    """The user has read the private-repo notice (SPEC -> Creating a session, step 2)."""
    await auth_store.ack_private_repo_notice(db, user.id)
    return Response(status_code=204)
