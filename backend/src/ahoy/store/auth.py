"""Store functions for users, GitHub user tokens and login sessions (SPEC -> Authentication).

Same conventions as store/repo.py: every call commits (reads too) and rolls back on error,
and timestamps come from the database clock (`clock_timestamp()`). Calls that compare
against "now" take an optional `now`; tests pass one to control time, the app passes None
unless a clock override is installed.

Tokens are stored only AES-256-GCM encrypted with `<user id>:<field>` as associated data; login
sessions are stored only as the SHA-256 of the cookie value.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, SecretStr
from sqlalchemy import DateTime, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.auth import crypto
from ahoy.github.user_client import GitHubUserInfo, UserTokens
from ahoy.harness.schemas import User, UserSettings
from ahoy.store.models import SHA256_BYTES, AuthSessionRow, GitHubTokensRow, UserRow
from ahoy.store.repo import _read, _write

IDLE_TIMEOUT = timedelta(days=7)
ABSOLUTE_LIFETIME = timedelta(days=30)
# last_seen_at is rewritten at most this often, so most requests do not write.
TOUCH_INTERVAL = timedelta(minutes=1)
USER_AGENT_MAX = 512


class AuthSession(BaseModel):
    user_id: UUID
    created_at: AwareDatetime
    last_seen_at: AwareDatetime
    user_agent: str
    checked_at: AwareDatetime  # the "now" of the read that returned this

    @property
    def expired(self) -> bool:
        return (
            self.last_seen_at <= self.checked_at - IDLE_TIMEOUT
            or self.created_at <= self.checked_at - ABSOLUTE_LIFETIME
        )

    @property
    def needs_touch(self) -> bool:
        return self.checked_at - self.last_seen_at >= TOUCH_INTERVAL


class StoredTokens(BaseModel):
    """Decrypted tokens, in memory only."""

    access_token: SecretStr
    access_expires_at: AwareDatetime
    refresh_token: SecretStr
    refresh_expires_at: AwareDatetime


def _now(now: datetime | None) -> Any:
    if now is None:
        return func.clock_timestamp(type_=DateTime(timezone=True))
    return literal(now, DateTime(timezone=True))


def _check_hash(id_hash: bytes) -> None:
    if len(id_hash) != SHA256_BYTES:
        raise ValueError(f"id_hash must be {SHA256_BYTES} bytes")


# --- users ---------------------------------------------------------------------------


async def upsert_user(db: AsyncSession, info: GitHubUserInfo, *, default_model: str) -> User:
    """Insert or update the user keyed by the numeric GitHub id (logins can be renamed).

    A new user gets default settings with `default_model`; an existing one keeps its
    settings and only has login and avatar refreshed.
    """
    avatar = info.avatar_url or ""
    settings = UserSettings(model=default_model).model_dump(mode="json")
    stmt = (
        pg_insert(UserRow)
        .values(
            github_id=info.id,
            github_login=info.login,
            avatar_url=avatar,
            settings=settings,
            created_at=func.clock_timestamp(),
        )
        .on_conflict_do_update(
            index_elements=[UserRow.github_id],
            set_={"github_login": info.login, "avatar_url": avatar},
        )
        .returning(*UserRow.__table__.c)
    )

    async def work() -> User:
        return User.model_validate(dict((await db.execute(stmt)).one()._mapping))

    return await _write(db, work)


async def get_user(db: AsyncSession, user_id: UUID) -> User | None:
    async def work() -> User | None:
        row = (
            await db.execute(select(*UserRow.__table__.c).where(UserRow.id == user_id))
        ).one_or_none()
        return None if row is None else User.model_validate(dict(row._mapping))

    return await _read(db, work)


async def get_user_by_github_id(db: AsyncSession, github_id: int) -> User | None:
    async def work() -> User | None:
        return await user_by_github_id(db, github_id)

    return await _read(db, work)


async def user_by_github_id(db: AsyncSession, github_id: int) -> User | None:
    """The user with this numeric GitHub id. No commit: for use inside a caller's transaction."""
    row = (
        await db.execute(select(*UserRow.__table__.c).where(UserRow.github_id == github_id))
    ).one_or_none()
    return None if row is None else User.model_validate(dict(row._mapping))


# --- GitHub user tokens --------------------------------------------------------------


async def save_tokens(db: AsyncSession, key: SecretStr, user_id: UUID, tokens: UserTokens) -> None:
    """Encrypt and store both tokens in one transaction, replacing any previous pair."""
    if tokens.refresh_token is None or tokens.expires_at is None:
        raise ValueError("GitHub user tokens must expire and come with a refresh token")
    if tokens.refresh_token_expires_at is None:
        raise ValueError("GitHub refresh token has no expiry")
    access_enc, access_nonce = crypto.encrypt(
        key, tokens.access_token.get_secret_value(), crypto.token_aad(user_id, "access")
    )
    refresh_enc, refresh_nonce = crypto.encrypt(
        key, tokens.refresh_token.get_secret_value(), crypto.token_aad(user_id, "refresh")
    )
    values = {
        "access_token_enc": access_enc,
        "access_token_nonce": access_nonce,
        "access_expires_at": tokens.expires_at,
        "refresh_token_enc": refresh_enc,
        "refresh_token_nonce": refresh_nonce,
        "refresh_expires_at": tokens.refresh_token_expires_at,
    }
    stmt = pg_insert(GitHubTokensRow).values(user_id=user_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=[GitHubTokensRow.user_id], set_=values)

    async def work() -> None:
        await db.execute(stmt)

    await _write(db, work)


async def get_tokens(db: AsyncSession, key: SecretStr, user_id: UUID) -> StoredTokens | None:
    """The user's decrypted tokens, or None. Raises CryptoError if they do not decrypt."""

    async def work() -> Any:
        return (
            await db.execute(
                select(GitHubTokensRow.__table__).where(GitHubTokensRow.user_id == user_id)
            )
        ).one_or_none()

    row = await _read(db, work)
    if row is None:
        return None
    return StoredTokens(
        access_token=crypto.decrypt(
            key, row.access_token_enc, row.access_token_nonce, crypto.token_aad(user_id, "access")
        ),
        access_expires_at=row.access_expires_at,
        refresh_token=crypto.decrypt(
            key,
            row.refresh_token_enc,
            row.refresh_token_nonce,
            crypto.token_aad(user_id, "refresh"),
        ),
        refresh_expires_at=row.refresh_expires_at,
    )


# --- login sessions ------------------------------------------------------------------


async def create_auth_session(
    db: AsyncSession,
    id_hash: bytes,
    user_id: UUID,
    *,
    user_agent: str = "",
    now: datetime | None = None,
) -> AuthSession:
    _check_hash(id_hash)
    at = _now(now)
    stmt = (
        pg_insert(AuthSessionRow)
        .values(
            id_hash=id_hash,
            user_id=user_id,
            created_at=at,
            last_seen_at=at,
            user_agent=user_agent[:USER_AGENT_MAX],
        )
        .returning(*AuthSessionRow.__table__.c, AuthSessionRow.created_at.label("checked_at"))
    )

    async def work() -> AuthSession:
        return AuthSession.model_validate(dict((await db.execute(stmt)).one()._mapping))

    return await _write(db, work)


async def get_auth_session(
    db: AsyncSession, id_hash: bytes, *, now: datetime | None = None
) -> AuthSession | None:
    """The live session for `id_hash`, or None. One SELECT; only a row found expired (idle
    7 d or older than 30 d) is deleted. `checked_at` is the "now" the check used."""
    _check_hash(id_hash)
    stmt = select(*AuthSessionRow.__table__.c, _now(now).label("checked_at")).where(
        AuthSessionRow.id_hash == id_hash
    )
    row = await _read(db, lambda: _one_or_none(db, stmt))
    if row is None:
        return None
    session = AuthSession.model_validate(dict(row._mapping))
    if not session.expired:
        return session

    async def work() -> None:
        await db.execute(delete(AuthSessionRow).where(AuthSessionRow.id_hash == id_hash))

    await _write(db, work)
    return None


async def _one_or_none(db: AsyncSession, stmt: Any) -> Any:
    return (await db.execute(stmt)).one_or_none()


async def touch_auth_session(
    db: AsyncSession, id_hash: bytes, *, now: datetime | None = None
) -> None:
    """Set last_seen_at to now. Callers touch only when `AuthSession.needs_touch`."""
    _check_hash(id_hash)
    stmt = update(AuthSessionRow).where(AuthSessionRow.id_hash == id_hash)
    stmt = stmt.values(last_seen_at=_now(now))

    async def work() -> None:
        await db.execute(stmt)

    await _write(db, work)


async def delete_auth_session(db: AsyncSession, id_hash: bytes) -> None:
    _check_hash(id_hash)

    async def work() -> None:
        await db.execute(delete(AuthSessionRow).where(AuthSessionRow.id_hash == id_hash))

    await _write(db, work)


async def delete_user_auth(db: AsyncSession, user_id: UUID) -> None:
    """Sign the user out everywhere: delete their tokens and every login session."""
    await _write(db, lambda: delete_user_auth_rows(db, user_id))


async def delete_user_auth_rows(db: AsyncSession, user_id: UUID) -> None:
    """`delete_user_auth` without the commit: for use inside a caller's transaction."""
    await db.execute(delete(AuthSessionRow).where(AuthSessionRow.user_id == user_id))
    await db.execute(delete(GitHubTokensRow).where(GitHubTokensRow.user_id == user_id))


async def ack_private_repo_notice(db: AsyncSession, user_id: UUID) -> None:
    """Record that the user acknowledged the private-repo notice (DB clock). Idempotent:
    the first acknowledgement's time is kept."""
    stmt = (
        update(UserRow)
        .where(UserRow.id == user_id, UserRow.private_repo_notice_ack_at.is_(None))
        .values(private_repo_notice_ack_at=func.clock_timestamp())
    )

    async def work() -> None:
        await db.execute(stmt)

    await _write(db, work)
