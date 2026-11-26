"""`GET /api/repos`: the repos the user may open (SPEC -> GitHub integration -> Listing repos).

`GET /user/installations`, then each installation's repositories, with the user's own
token: exactly the repos the user may open. Cached per user for 60 s (in-process, bounded).
Suspended installations are skipped (they cannot mint tokens). GitHub errors (rate limits,
5xx, network) are 503 and never cached.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.auth.deps import CurrentUser, user_token
from ahoy.auth.sessions import DB, Auth, AuthState, clear_session_cookie_header
from ahoy.github.http import GitHubError
from ahoy.github.user_client import GitHubUser
from ahoy.harness.schemas import User

LIST_TTL_S = 60.0
MAX_USERS = 10_000

router = APIRouter(prefix="/api")


class RepoOut(BaseModel):
    id: int
    full_name: str
    private: bool
    default_branch: str
    installation_id: int


class ReposOut(BaseModel):
    repos: list[RepoOut]
    install_url: str


@dataclass(frozen=True)
class _Listed:
    repos: tuple[RepoOut, ...]
    expires_at: float


class RepoListCache:
    """Per-user repo listings, valid for `ttl` seconds; at most `max_users` (oldest dropped).

    `clock` is read on every call, so tests can replace it after construction."""

    def __init__(
        self,
        github: GitHubUser,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = LIST_TTL_S,
        max_users: int = MAX_USERS,
    ) -> None:
        self.github = github
        self.clock = clock
        self._ttl = ttl
        self._max = max_users
        self._entries: dict[object, _Listed] = {}  # insertion order = expiry order

    def __len__(self) -> int:
        return len(self._entries)

    def invalidate_user(self, user_id: object) -> None:
        self._entries.pop(user_id, None)

    def clear(self) -> None:
        """Forget every listing (installation webhooks: who sees what may have changed)."""
        self._entries.clear()

    async def repos(self, auth: AuthState, db: AsyncSession, user: User) -> list[RepoOut]:
        """The user's repos. Raises 503 when GitHub cannot answer, 401 like `user_token`."""
        hit = self._entries.get(user.id)
        if hit is not None and hit.expires_at > self.clock():
            return list(hit.repos)
        try:
            token = await user_token(auth, db, user)
        except HTTPException as exc:
            if exc.status_code == 401:
                # The user's sessions just ended (refresh refused): clear the cookie too.
                exc.headers = {
                    **(exc.headers or {}),
                    "set-cookie": clear_session_cookie_header(auth.settings),
                }
            raise
        try:
            found: list[RepoOut] = []
            for inst in await self.github.list_installations(token):
                if inst.suspended:
                    continue
                for r in await self.github.list_installation_repos(token, inst.id):
                    found.append(
                        RepoOut(
                            id=r.id,
                            full_name=r.full_name,
                            private=r.private,
                            default_branch=r.default_branch,
                            installation_id=inst.id,
                        )
                    )
        except GitHubError:
            raise HTTPException(503, "GitHub is unavailable, try again") from None
        self._entries.pop(user.id, None)
        self._entries[user.id] = _Listed(tuple(found), self.clock() + self._ttl)
        while len(self._entries) > self._max:
            del self._entries[next(iter(self._entries))]
        return found


def repo_list(request: Request) -> RepoListCache:
    cache: RepoListCache = request.app.state.repo_list
    return cache


def install_url(auth: AuthState) -> str:
    return f"https://github.com/apps/{auth.settings.github_app_slug or ''}/installations/new"


@router.get("/repos")
async def list_repos(request: Request, user: CurrentUser, auth: Auth, db: DB) -> ReposOut:
    repos = await repo_list(request).repos(auth, db, user)
    return ReposOut(repos=repos, install_url=install_url(auth))
