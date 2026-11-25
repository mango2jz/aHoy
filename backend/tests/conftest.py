"""Shared test setup.

- AHOY_ENV=test for the whole run (Settings defaults to prod, which needs an https origin).
- The test database: once per run, connected as ahoy_owner through TEST_MIGRATION_DATABASE_URL,
  drop and recreate ahoy_test and migrate it to head. Each DB test then gets `db`, an ahoy_app
  AsyncSession (TEST_DATABASE_URL) inside an outer transaction that is rolled back afterwards.
  The session joins it with SAVEPOINTs, so code under test may commit and roll back freely.
- DB tests are marked `db` (use `pytestmark = pytest.mark.db`). If Postgres is unreachable
  they skip with the reason, unless AHOY_REQUIRE_DB=1 (CI), which makes that an error.
  Other tests never connect.
"""

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ahoy.store.db import async_url, make_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]

# docker-compose.yml dev defaults. CI overrides both variables.
DEFAULT_MIGRATION_URL = "postgresql+asyncpg://ahoy_owner:ahoy_owner_dev@127.0.0.1:5432/ahoy_test"
DEFAULT_APP_URL = "postgresql+asyncpg://ahoy_app:ahoy_app_dev@127.0.0.1:5432/ahoy_test"
# Drop/create run from this database; PUBLIC may connect to it on a stock server.
MAINTENANCE_DB = "postgres"
CONNECT_TIMEOUT_S = 3
_TARGET_QUERY_KEYS = frozenset({"database", "dbname", "host", "port", "user", "dsn"})

_saved_env: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _saved_env
    _saved_env = os.environ.get("AHOY_ENV")
    os.environ["AHOY_ENV"] = "test"
    config.addinivalue_line("markers", "db: needs the Postgres test database (ahoy_test)")


def pytest_unconfigure(config: pytest.Config) -> None:
    if _saved_env is None:
        os.environ.pop("AHOY_ENV", None)
    else:
        os.environ["AHOY_ENV"] = _saved_env


def _test_urls() -> tuple[URL, URL]:
    owner = make_url(
        async_url(os.environ.get("TEST_MIGRATION_DATABASE_URL", DEFAULT_MIGRATION_URL))
    )
    app = make_url(async_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_APP_URL)))
    # asyncpg takes query parameters as connect arguments, so ?database=, ?host= and the like
    # would point the run somewhere else than the checks below see.
    for url in (owner, app):
        overrides = sorted(set(url.query) & _TARGET_QUERY_KEYS)
        if overrides:
            raise pytest.UsageError(
                f"test database URLs must not set {', '.join(overrides)} as query parameters"
            )
    if (owner.host, owner.port) != (app.host, app.port):
        raise pytest.UsageError(
            "TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL name different servers"
        )
    name = owner.database or ""
    # The database is dropped: refuse anything that is not clearly a test database.
    if not name.endswith("_test"):
        raise pytest.UsageError(
            f"TEST_MIGRATION_DATABASE_URL must name a *_test database: {name!r}"
        )
    if app.database != name:
        raise pytest.UsageError(
            "TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL name different databases"
        )
    return owner, app


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _recreate(owner: URL) -> None:
    name = _quote(owner.database or "")
    engine = create_async_engine(
        owner.set(database=MAINTENANCE_DB),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": CONNECT_TIMEOUT_S},
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {name}"))
            await conn.execute(text(f"REVOKE CONNECT ON DATABASE {name} FROM PUBLIC"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {name} TO ahoy_app, ahoy_owner"))
    finally:
        await engine.dispose()
    # Same as deploy/db/bootstrap.sh (already the default on Postgres 15+).
    engine = create_async_engine(owner, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    finally:
        await engine.dispose()


def _migrate(owner: URL) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    # ConfigParser interpolates %, so escape it in the password.
    cfg.set_main_option(
        "sqlalchemy.url", owner.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(cfg, "head")


def _unreachable(exc: BaseException) -> bool:
    return isinstance(exc, OSError | TimeoutError) or isinstance(exc.__cause__, OSError)


@pytest.fixture(scope="session")
def test_database() -> Iterator[URL]:
    """Recreate and migrate the test database once per run; yields the ahoy_app URL."""
    owner, app = _test_urls()
    try:
        asyncio.run(_recreate(owner))
    except Exception as exc:
        if not _unreachable(exc):
            raise
        reason = (
            f"Postgres unreachable at {owner.host}:{owner.port} ({exc!r}); "
            "start it with `docker compose up -d postgres`"
        )
        if os.environ.get("AHOY_REQUIRE_DB") == "1":
            pytest.fail(f"AHOY_REQUIRE_DB=1: {reason}", pytrace=False)
        pytest.skip(reason)
    _migrate(owner)
    yield app


@pytest.fixture
async def db(test_database: URL) -> AsyncIterator[AsyncSession]:
    """An ahoy_app session in an outer transaction that is rolled back after the test."""
    engine = make_engine(test_database, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            session = AsyncSession(
                bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            try:
                yield session
            finally:
                await session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def make_user(db: AsyncSession) -> Callable[[], Awaitable[UUID]]:
    """Insert a users row (ahoy_app may) and return its id. Not committed past the test."""
    counter = itertools.count(1)

    async def make() -> UUID:
        n = next(counter)
        user_id = uuid4()
        await db.execute(
            text(
                "INSERT INTO users (id, github_id, github_login, avatar_url, settings)"
                " VALUES (:id, :gid, :login, '', '{}'::jsonb)"
            ),
            {"id": user_id, "gid": 1_000_000 + n, "login": f"user{n}"},
        )
        return user_id

    return make
