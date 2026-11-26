"""Login sessions (SPEC -> Authentication -> Sessions): the cookie, its row, sign-in, logout.

The cookie value is 256 random bits; only its SHA-256 is stored. Production uses the
`__Host-` cookie (Secure, HttpOnly, SameSite=Lax, Path=/, no Domain). With AHOY_ENV dev or
test over plain http the cookie drops Secure and therefore the prefix.

`AuthState` (on `app.state.auth`) carries what the auth code needs at request time: the
settings, the token key, the GitHub user client, the per-user refresh locks, the repo-access
cache (authz.py) and an optional clock override for tests.
"""

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ahoy.config import Settings
from ahoy.github.user_client import GitHubUser, UserTokens
from ahoy.harness.schemas import User
from ahoy.store import auth as auth_store
from ahoy.store.auth import ABSOLUTE_LIFETIME

if TYPE_CHECKING:
    from ahoy.authz import AccessCache

SESSION_COOKIE_PROD = "__Host-ahoy_sid"
SESSION_COOKIE_DEV = "ahoy_sid"
# The signed pre-auth cookie (state + PKCE verifier) that T20's login route sets.
PREAUTH_COOKIE_PROD = "__Host-ahoy_preauth"
PREAUTH_COOKIE_DEV = "ahoy_preauth"
COOKIE_VALUE_BYTES = 32

Clock = Callable[[], datetime]


@dataclass
class AuthState:
    settings: Settings
    key: SecretStr
    github: GitHubUser
    default_model: str
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    # Tests install a clock; then it also replaces the DB clock for login-session times.
    clock: Clock | None = None
    # Repo-access answers (authz.py); create_app installs one.
    access: "AccessCache | None" = None
    _locks: WeakValueDictionary[UUID, asyncio.Lock] = field(
        default_factory=WeakValueDictionary, repr=False
    )
    # Strong references to shielded token refreshes, so they finish if the request is cancelled.
    tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)

    def now(self) -> datetime:
        return self.clock() if self.clock is not None else datetime.now(UTC)

    def db_now(self) -> datetime | None:
        """`now` for store calls: None means the database clock."""
        return self.clock() if self.clock is not None else None

    def refresh_lock(self, user_id: UUID) -> asyncio.Lock:
        """The user's refresh lock. Held strongly only while someone uses it."""
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock


def get_auth(request: Request) -> AuthState:
    state: AuthState = request.app.state.auth
    return state


Auth = Annotated[AuthState, Depends(get_auth)]


async def get_db(auth: Auth) -> AsyncIterator[AsyncSession]:
    if auth.sessionmaker is None:
        raise HTTPException(503, "database not configured")
    async with auth.sessionmaker() as db:
        yield db


DB = Annotated[AsyncSession, Depends(get_db)]


def _secure(settings: Settings) -> bool:
    return settings.ahoy_env == "prod"


def session_cookie_name(settings: Settings) -> str:
    return SESSION_COOKIE_PROD if _secure(settings) else SESSION_COOKIE_DEV


def preauth_cookie_name(settings: Settings) -> str:
    return PREAUTH_COOKIE_PROD if _secure(settings) else PREAUTH_COOKIE_DEV


def hash_cookie(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _clear(response: Response, name: str, settings: Settings) -> None:
    response.delete_cookie(name, path="/", secure=_secure(settings), httponly=True, samesite="lax")


def clear_session_cookie(response: Response, settings: Settings) -> None:
    _clear(response, session_cookie_name(settings), settings)


def clear_session_cookie_header(settings: Settings) -> str:
    """The Set-Cookie value that clears the session cookie (for error responses)."""
    response = Response()
    clear_session_cookie(response, settings)
    return response.headers["set-cookie"]


async def start(request: Request, db: AsyncSession, user: User, tokens: UserTokens) -> Response:
    """Sign `user` in: store the tokens, open a new login session, set its cookie, drop the
    pre-auth cookie and redirect to `/`. Every call issues a new cookie value."""
    auth = get_auth(request)
    settings = auth.settings
    await auth_store.save_tokens(db, auth.key, user.id, tokens)
    # A cookie the browser already carried is replaced, so its row goes too.
    old = request.cookies.get(session_cookie_name(settings))
    if old:
        await auth_store.delete_auth_session(db, hash_cookie(old))
    value = secrets.token_urlsafe(COOKIE_VALUE_BYTES)
    await auth_store.create_auth_session(
        db,
        hash_cookie(value),
        user.id,
        user_agent=request.headers.get("user-agent", ""),
        now=auth.db_now(),
    )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        session_cookie_name(settings),
        value,
        max_age=int(ABSOLUTE_LIFETIME.total_seconds()),
        path="/",
        secure=_secure(settings),
        httponly=True,
        samesite="lax",
    )
    _clear(response, preauth_cookie_name(settings), settings)
    return response


router = APIRouter()


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, auth: Auth, db: DB) -> Response:
    value = request.cookies.get(session_cookie_name(auth.settings))
    if value:
        await auth_store.delete_auth_session(db, hash_cookie(value))
    response = Response(status_code=204)
    clear_session_cookie(response, auth.settings)
    return response
