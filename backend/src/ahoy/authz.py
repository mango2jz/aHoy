"""Authorization (SPEC -> Authorization, D14): the one door to a session.

Every route that touches a session, its nodes, files or events gets it from
`load_session(...)`; no route checks ownership on its own. The rules, in this order:

1. No such session -> 404.
2. Not the owner and not shared -> 404, before any GitHub call (ids reveal nothing).
3. The owner deleting -> allowed without a repo check (US15: owners can always delete).
4. Everything else needs current repo access for the *acting* user, else 404 (the session
   is hidden, not deleted, and reappears when access returns). This comes before the 403
   below, so a user who may not see the session cannot learn that it exists.
5. Not the owner (shared, with repo access) and an owner-only action (steer, allow
   blocked, toggle sharing, delete) -> 403.

Repo access has two parts, asked together and cached together for 5 minutes:
the user's own token can read the repo (`can_read_repo`, by numeric id) and the session's
installation can still mint a token for it (`can_mint_for`). The cache key is
`(user_id, repo_id, installation_id)`: two sessions on the same repo through different
installations must not share an answer, because part 2 differs between them. Both True
and False are cached. GitHub errors (rate limits, 5xx, network) become 503 and are never
cached or read as "no access". `invalidate(repo_id)` and `invalidate_user(user_id)` drop
entries early (webhooks; `invalidate_user` also on every sign-in), and `fresh=True` asks
GitHub and refreshes the entry (SSE re-checks). Concurrent misses for one key share one
GitHub check, and the cache holds at most `max_entries` entries (oldest dropped first).

Ordering: every check gets a sequence number when it starts. While checks for a key are in
flight, the key keeps a stamp: the sequence number of its newest stored answer or
invalidation. A check that lands with an older number than the stamp is not stored, so an
older normal check never overwrites a later-started fresh check, and an answer asked before
an invalidation never survives it. Invalidations stamp only the keys they match.
"""

import asyncio
import itertools
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.auth.deps import user_token
from ahoy.auth.sessions import AuthState, clear_session_cookie_header, get_auth
from ahoy.github.app_client import GitHubApp
from ahoy.github.http import GitHubError
from ahoy.github.user_client import GitHubUser
from ahoy.harness.schemas import Session, User
from ahoy.store import repo

ACCESS_TTL = timedelta(seconds=300)
MAX_ENTRIES = 10_000

AccessKey = tuple[UUID, int, int]
"""(user_id, repo_id, installation_id)"""


class Action(StrEnum):
    VIEW = "view"
    READ_FILES = "read_files"
    SUBSCRIBE = "subscribe"
    STEER = "steer"
    ALLOW_BLOCKED = "allow_blocked"
    TOGGLE_SHARE = "toggle_share"
    DELETE = "delete"


OWNER_ONLY = frozenset({Action.STEER, Action.ALLOW_BLOCKED, Action.TOGGLE_SHARE, Action.DELETE})


@dataclass(frozen=True)
class _Entry:
    allowed: bool
    expires_at: datetime


class AccessCache:
    """Repo-access answers per (user, repo, installation), valid for `ttl`."""

    def __init__(
        self,
        user_client: GitHubUser,
        app_client: GitHubApp,
        *,
        clock: Callable[[], datetime],
        ttl: timedelta = ACCESS_TTL,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._user = user_client
        self._app = app_client
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: dict[AccessKey, _Entry] = {}  # insertion order = expiry order
        self._inflight: dict[AccessKey, asyncio.Future[bool]] = {}  # joinable normal checks
        self._seq = itertools.count()
        self._pending: dict[AccessKey, int] = {}  # running checks (normal and fresh) per key
        self._stamp: dict[AccessKey, int] = {}  # newest store/invalidation, keys in _pending

    def __len__(self) -> int:
        return len(self._entries)

    def cached(self, key: AccessKey) -> bool | None:
        """The cached answer, or None when there is none or it has expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return entry.allowed

    async def check(self, key: AccessKey, token: SecretStr, *, fresh: bool = False) -> bool:
        """Ask GitHub and store the answer. Raises GitHubError (nothing is stored then).

        Without `fresh`, a check already running for `key` is joined instead of repeated.
        """
        if fresh:
            seq = self._begin(key)
            try:
                return await self._ask_and_store(key, token, seq)
            finally:
                self._end(key)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._ask_and_store(key, token, self._begin(key)))
            self._inflight[key] = task
            task.add_done_callback(partial(self._done, key))
        # Shielded: a cancelled request must not cancel the check others are waiting on.
        return await asyncio.shield(task)

    def _begin(self, key: AccessKey) -> int:
        """Number a check as it starts (synchronously, before any await)."""
        self._pending[key] = self._pending.get(key, 0) + 1
        return next(self._seq)

    def _end(self, key: AccessKey) -> None:
        left = self._pending[key] - 1
        if left:
            self._pending[key] = left
        else:
            # No older check can land any more, so the stamp is no longer needed.
            del self._pending[key]
            self._stamp.pop(key, None)

    def _done(self, key: AccessKey, task: asyncio.Future[bool]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        self._end(key)
        with suppress(asyncio.CancelledError):
            task.exception()  # mark seen when every waiter was cancelled

    async def _ask_and_store(self, key: AccessKey, token: SecretStr, seq: int) -> bool:
        _user_id, repo_id, installation_id = key
        allowed = await self._user.can_read_repo(token, repo_id) and await self._app.can_mint_for(
            installation_id, repo_id
        )
        # A newer answer or an invalidation arrived meanwhile: it wins.
        if seq > self._stamp.get(key, -1):
            self._stamp[key] = seq
            self._store(key, allowed)
        return allowed

    def _store(self, key: AccessKey, allowed: bool) -> None:
        self._entries.pop(key, None)
        self._entries[key] = _Entry(allowed, self._clock() + self._ttl)
        while len(self._entries) > self._max:
            del self._entries[next(iter(self._entries))]

    def _drop(self, match: Callable[[AccessKey], bool]) -> None:
        for key in [k for k in self._entries if match(k)]:
            del self._entries[key]
        for key in [k for k in self._inflight if match(k)]:
            del self._inflight[key]  # new callers start a new check
        stamp = next(self._seq)
        for key in self._pending:
            if match(key):
                self._stamp[key] = stamp  # checks started before now must not store

    def invalidate(self, repo_id: int) -> None:
        """Forget every answer for this repo (any user, any installation)."""
        self._drop(lambda key: key[1] == repo_id)

    def invalidate_user(self, user_id: UUID) -> None:
        """Forget every answer for this user."""
        self._drop(lambda key: key[0] == user_id)

    def invalidate_installation(self, installation_id: int) -> None:
        """Forget every answer through this installation (any user, any repo)."""
        self._drop(lambda key: key[2] == installation_id)


def _not_found() -> HTTPException:
    return HTTPException(404, "not found")


def access_cache(auth: AuthState) -> AccessCache:
    if auth.access is None:
        raise HTTPException(503, "authorization is not configured")
    return auth.access


async def has_repo_access(
    auth: AuthState, db: AsyncSession, user: User, session: Session, *, fresh: bool = False
) -> bool:
    """Whether `user` currently has both parts of repo access for `session` (cached 5 min).

    Raises 503 when GitHub cannot answer, and 401 when the user's token is unusable.
    """
    return await check_repo_access(
        auth, db, user, session.repo_id, session.installation_id, fresh=fresh
    )


async def check_repo_access(
    auth: AuthState,
    db: AsyncSession,
    user: User,
    repo_id: int,
    installation_id: int,
    *,
    fresh: bool = False,
) -> bool:
    """`has_repo_access` before a session exists (session creation): same cache entry."""
    cache = access_cache(auth)
    key: AccessKey = (user.id, repo_id, installation_id)
    if not fresh:
        hit = cache.cached(key)
        if hit is not None:
            return hit
    try:
        token = await user_token(auth, db, user)
    except HTTPException as exc:
        if exc.status_code == 401:
            # The user's sessions just ended (refresh refused): clear the cookie too, as
            # current_user does.
            exc.headers = {
                **(exc.headers or {}),
                "set-cookie": clear_session_cookie_header(auth.settings),
            }
        raise
    try:
        return await cache.check(key, token, fresh=fresh)
    except GitHubError:
        raise HTTPException(503, "GitHub is unavailable, try again") from None


async def authorize(
    auth: AuthState,
    db: AsyncSession,
    user: User,
    session: Session,
    action: Action,
    *,
    fresh: bool = False,
) -> None:
    """Raise 404 or 403 (or 503 when GitHub cannot answer). Never partial access."""
    owner = session.owner_id == user.id
    if owner and action is Action.DELETE:
        return
    if not owner and not session.shared:
        raise _not_found()
    if not await has_repo_access(auth, db, user, session, fresh=fresh):
        raise _not_found()
    if not owner and action in OWNER_ONLY:
        raise HTTPException(403, "only the session owner can do this")


async def load_session(
    request_or_auth: Request | AuthState,
    db: AsyncSession,
    user: User,
    session_id: UUID,
    action: Action,
    *,
    fresh: bool = False,
) -> Session:
    """The session if `user` may do `action` on it; otherwise 404 / 403 per SPEC."""
    auth = request_or_auth if isinstance(request_or_auth, AuthState) else get_auth(request_or_auth)
    session = await repo.get_session(db, session_id)
    if session is None:
        raise _not_found()
    await authorize(auth, db, user, session, action, fresh=fresh)
    return session
