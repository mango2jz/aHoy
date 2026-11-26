"""Request dependencies: the signed-in user and their (fresh) GitHub user token.

GitHub user tokens live 8 h and refresh tokens are single-use and rotate, so a refresh must
happen at most once per expiry even when several requests notice it at the same moment.
`user_token` refreshes under a per-user asyncio.Lock (one backend instance): take the lock,
re-read the row, refresh only if still near expiry, and save both new tokens in one
transaction. If GitHub refuses the refresh, every login session of that user ends (their
tokens are useless now) and the request gets 401.

The locked part runs as a shielded task with its own DB session: once GitHub has rotated the
refresh token, the new pair must be saved even if the client disconnects, or the user would
be left with a consumed refresh token.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.auth.crypto import CryptoError
from ahoy.auth.sessions import (
    DB,
    Auth,
    AuthState,
    clear_session_cookie_header,
    hash_cookie,
    session_cookie_name,
)
from ahoy.github.http import GitHubAuthError, GitHubError
from ahoy.github.user_client import UserTokens
from ahoy.harness.schemas import User
from ahoy.store import auth as auth_store
from ahoy.store.auth import StoredTokens

REFRESH_MARGIN = timedelta(minutes=5)


def _unauthorized() -> HTTPException:
    return HTTPException(401, "not signed in")


def _unavailable() -> HTTPException:
    return HTTPException(503, "GitHub is unavailable, try again")


async def _end(db: AsyncSession, user: User) -> HTTPException:
    await auth_store.delete_user_auth(db, user.id)
    return _unauthorized()


async def _read_tokens(auth: AuthState, db: AsyncSession, user: User) -> StoredTokens | None:
    try:
        return await auth_store.get_tokens(db, auth.key, user.id)
    except CryptoError:
        # Undecryptable (e.g. the key changed): same as no tokens.
        return None


@asynccontextmanager
async def _own_session(auth: AuthState) -> AsyncIterator[AsyncSession]:
    """A DB session that outlives the request (never the request's own)."""
    if auth.sessionmaker is None:
        raise HTTPException(503, "database not configured")
    async with auth.sessionmaker() as db:
        yield db


async def _save(db: AsyncSession, auth: AuthState, user: User, fresh: UserTokens) -> None:
    """Save the rotated pair, retrying once: GitHub already consumed the old refresh token."""
    try:
        await auth_store.save_tokens(db, auth.key, user.id, fresh)
    except ValueError:
        raise
    except Exception:
        await auth_store.save_tokens(db, auth.key, user.id, fresh)


async def _refresh_locked(auth: AuthState, user: User) -> SecretStr:
    async with auth.refresh_lock(user.id), _own_session(auth) as db:
        # Someone holding the lock before us may have refreshed already.
        tokens = await _read_tokens(auth, db, user)
        if tokens is None:
            raise await _end(db, user)
        now = auth.now()
        if tokens.access_expires_at - now > REFRESH_MARGIN:
            return tokens.access_token
        if tokens.refresh_expires_at <= now:
            raise await _end(db, user)
        try:
            fresh = await auth.github.refresh(tokens.refresh_token)
        except GitHubAuthError:
            raise await _end(db, user) from None
        except GitHubError:
            raise _unavailable() from None
        try:
            await _save(db, auth, user, fresh)
        except ValueError:
            # GitHub answered without a refresh token or expiry: cannot keep this session.
            raise await _end(db, user) from None
        except Exception:
            raise _unavailable() from None
        return fresh.access_token


def _finished(auth: AuthState) -> Callable[[asyncio.Task[SecretStr]], None]:
    def done(task: asyncio.Task[SecretStr]) -> None:
        auth.tasks.discard(task)
        # Mark the outcome as seen: if the request was cancelled nobody awaits it.
        if not task.cancelled():
            task.exception()

    return done


async def user_token(auth: AuthState, db: AsyncSession, user: User) -> SecretStr:
    """The user's GitHub access token, refreshed first if it expires within 5 minutes.

    Raises 401 (after ending the user's sessions) when there are no usable tokens or GitHub
    refuses the refresh, and 503 when GitHub cannot be reached (sessions are kept).
    """
    tokens = await _read_tokens(auth, db, user)
    if tokens is None:
        raise await _end(db, user)
    if tokens.access_expires_at - auth.now() > REFRESH_MARGIN:
        return tokens.access_token
    task = asyncio.ensure_future(_refresh_locked(auth, user))
    auth.tasks.add(task)
    task.add_done_callback(_finished(auth))
    return await asyncio.shield(task)


async def current_user(request: Request, auth: Auth, db: DB) -> User:
    """The signed-in user (401 otherwise, clearing a cookie the request carried). Also keeps
    their GitHub token fresh. A steady-state request only reads."""
    value = request.cookies.get(session_cookie_name(auth.settings))
    if not value:
        raise _unauthorized()
    try:
        user = await _signed_in(auth, db, value)
    except HTTPException as exc:
        if exc.status_code == 401:
            exc.headers = {
                **(exc.headers or {}),
                "set-cookie": clear_session_cookie_header(auth.settings),
            }
        raise
    request.state.user_id = user.id  # for the request log line (security.RequestLogMiddleware)
    return user


async def _signed_in(auth: AuthState, db: AsyncSession, value: str) -> User:
    id_hash = hash_cookie(value)
    now = auth.db_now()
    session = await auth_store.get_auth_session(db, id_hash, now=now)
    if session is None:
        raise _unauthorized()
    user = await auth_store.get_user(db, session.user_id)
    if user is None:
        raise _unauthorized()
    await user_token(auth, db, user)
    if session.needs_touch:
        await auth_store.touch_auth_session(db, id_hash, now=now)
    return user


CurrentUser = Annotated[User, Depends(current_user)]
