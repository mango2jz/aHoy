"""Shared test setup.

- AHOY_ENV=test for the whole run (Settings defaults to prod, which needs an https origin).
- The test database: once per run, connected as ahoy_owner through TEST_MIGRATION_DATABASE_URL,
  create a database of its own and migrate it to head. Its name is the configured `*_test`
  name plus the pid (`ahoy_test_<pid>`), or AHOY_TEST_DB_NAME. That keeps concurrent runs on
  one machine apart; pids are unique only within one pid namespace, so containers or
  machines sharing a Postgres server must each set their own AHOY_TEST_DB_NAME (the stale
  cleanup below would otherwise drop another host's live database). It is dropped at the
  end of the run, and leftovers whose pid is not alive on this host are dropped at the start
  (both best effort). See .env.example. Each DB test then gets `db`, an ahoy_app
  AsyncSession (TEST_DATABASE_URL, same server, run database) inside an outer transaction
  that is rolled back afterwards.
  The session joins it with SAVEPOINTs, so code under test may commit and roll back freely.
- DB tests are marked `db` (use `pytestmark = pytest.mark.db`). If Postgres is unreachable
  they skip with the reason, unless AHOY_REQUIRE_DB=1 (CI), which makes that an error.
  Other tests never connect.
- `SameSession` hands the test's one session to code that opens its own (AuthState's
  sessionmaker). `sign_up` and `make_session` create mock-GitHub users (with stored tokens)
  and sessions on the mock repos.
"""

import asyncio
import itertools
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ahoy.auth.sessions import AuthState
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.harness.schemas import Session, User
from ahoy.store import auth as auth_store
from ahoy.store import repo
from ahoy.store.db import async_url, make_engine
from ahoy.store.models import SessionRow

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


_RUN_DB_NAME = re.compile(r"[a-z0-9_]+_test(_[0-9]+)?")


def _run_db_name(base: str) -> str:
    """This run's database: AHOY_TEST_DB_NAME, else `<base>_<pid>`."""
    name = os.environ.get("AHOY_TEST_DB_NAME") or f"{base}_{os.getpid()}"
    # It is dropped: refuse anything that is not clearly a test database.
    if not _RUN_DB_NAME.fullmatch(name):
        raise pytest.UsageError(f"AHOY_TEST_DB_NAME must look like *_test or *_test_<n>: {name!r}")
    return name


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True  # never signal a process group; treat as in use
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. PermissionError: exists, owned by someone else
    return True


def _maintenance_engine(owner: URL) -> Any:
    return create_async_engine(
        owner.set(database=MAINTENANCE_DB),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": CONNECT_TIMEOUT_S},
    )


async def _drop_stale(conn: Any, base: str) -> None:
    """Drop `<base>_<pid>` databases of runs that are gone (pids on this host). Best effort."""
    rows = await conn.execute(text("SELECT datname FROM pg_database"))
    pattern = re.compile(re.escape(base) + r"_([0-9]+)")
    for (datname,) in rows.all():
        match = pattern.fullmatch(datname)
        if match is None or _pid_alive(int(match.group(1))):
            continue
        try:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_quote(datname)} WITH (FORCE)"))
        except Exception:  # another run may be dropping it too
            await conn.rollback()


async def _drop(owner: URL) -> None:
    engine = _maintenance_engine(owner)
    try:
        async with engine.connect() as conn:
            name = _quote(owner.database or "")
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        await engine.dispose()


async def _recreate(owner: URL, base: str) -> None:
    name = _quote(owner.database or "")
    engine = _maintenance_engine(owner)
    try:
        async with engine.connect() as conn:
            try:
                await _drop_stale(conn, base)
            except Exception:  # cleanup must never block the run
                await conn.rollback()
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
    """Create and migrate this run's test database; yields the ahoy_app URL. Dropped after."""
    owner, app = _test_urls()
    base = owner.database or ""
    name = _run_db_name(base)
    owner, app = owner.set(database=name), app.set(database=name)
    try:
        asyncio.run(_recreate(owner, base))
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
    try:
        _migrate(owner)
        yield app
    finally:
        try:
            asyncio.run(_drop(owner))
        except Exception as exc:  # best effort: the next run's stale cleanup retries
            print(f"could not drop test database {name}: {exc!r}")


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


class SameSession:
    """A sessionmaker stand-in that hands out the test's one (rolled-back) session and
    leaves closing it to the `db` fixture."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def __call__(self) -> "SameSession":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self.db

    async def __aexit__(self, *exc: object) -> None:
        return None

    def maker(self) -> async_sessionmaker[AsyncSession]:
        return cast(async_sessionmaker[AsyncSession], self)


SignUp = Callable[[AuthState, str], Awaitable[User]]
MakeSession = Callable[..., Awaitable[Session]]


@pytest.fixture
def sign_up(db: AsyncSession) -> SignUp:
    """Sign a mock-GitHub user (alice, bob, mallory) up: user row plus stored tokens."""

    async def make(auth: AuthState, name: str) -> User:
        gh = cast(MockGitHub, auth.github)
        tokens = await gh.exchange_code(gh.issue_code(name), "", "")
        info = await gh.get_user(tokens.access_token)
        user = await auth_store.upsert_user(db, info, default_model="mock")
        await auth_store.save_tokens(db, auth.key, user.id, tokens)
        return user

    return make


@pytest.fixture
def make_session(db: AsyncSession) -> MakeSession:
    """Create a ready session owned by `owner` on a mock repo (default: private fixture/demo)."""

    async def make(
        owner: User,
        *,
        repo_id: int = 7001,
        installation_id: int = INSTALLATION_ID,
        shared: bool = False,
    ) -> Session:
        session, _root = await repo.create_session(
            db,
            owner_id=owner.id,
            installation_id=installation_id,
            repo_id=repo_id,
            repo_full_name="fixture/demo" if repo_id == 7001 else "fixture/public",
            ref="main",
            commit_sha="0" * 40,
            private=repo_id == 7001,
            model="mock",
            status="ready",
        )
        if shared:
            await db.execute(
                update(SessionRow).where(SessionRow.id == session.id).values(shared=True)
            )
            await db.commit()
            session = session.model_copy(update={"shared": True})
        return session

    return make
