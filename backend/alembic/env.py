"""Alembic environment (async, asyncpg). Migrations run as ahoy_owner.

The URL comes from, in order: a caller-set `sqlalchemy.url` (e.g. the test suite pointing at
ahoy_test via `Config.set_main_option`), else MIGRATION_DATABASE_URL from the environment or
.env. A caller can also pass an open connection in `config.attributes["connection"]`.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from ahoy.config import Settings
from ahoy.store.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


class _MigrationSettings(BaseSettings):
    """Only the migration URL, from the same env files as Settings, without the app's
    other validation (migrations do not need APP_ORIGIN or provider secrets)."""

    model_config = SettingsConfigDict(
        env_file=Settings.model_config.get("env_file"), extra="ignore"
    )

    migration_database_url: SecretStr | None = None


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        secret = _MigrationSettings().migration_database_url
        url = secret.get_secret_value() if secret is not None else ""
    if not url:
        raise RuntimeError(
            "MIGRATION_DATABASE_URL is not set (it must point at the ahoy_owner role)"
        )
    parsed = make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
