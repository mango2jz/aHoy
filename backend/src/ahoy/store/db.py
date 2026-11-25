"""Async engine and session factory for the app role (DATABASE_URL, ahoy_app).

Nothing connects at import time: the app builds its engine at startup and tests build their
own against ahoy_test.
"""

from typing import Any

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def async_url(url: str | SecretStr) -> str:
    """The URL with the asyncpg driver (a bare postgresql:// URL gets +asyncpg)."""
    raw = url.get_secret_value() if isinstance(url, SecretStr) else url
    parsed = make_url(raw)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError(f"unsupported database driver {parsed.drivername!r} (want asyncpg)")
    return parsed.render_as_string(hide_password=False)


def make_engine(url: str | SecretStr, **kwargs: Any) -> AsyncEngine:
    """An async engine for DATABASE_URL. Extra keyword arguments go to create_async_engine."""
    return create_async_engine(async_url(url), **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: the store returns Pydantic models, but rows read after a commit
    # must not trigger lazy reloads on a closed transaction.
    return async_sessionmaker(engine, expire_on_commit=False)
