"""Alembic environment: async engine, URL from app settings, advisory lock.

The app runs migrations on startup (src/migrate.py), so several replicas may
start at once. A Postgres advisory lock makes them take turns; the ones that
wait find nothing left to do.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.config import get_settings
from src.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Arbitrary constant shared by every instance of this app
MIGRATION_LOCK_ID = 0x63616B5F6D6967  # "cak_mig"


def database_url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url_str


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (alembic upgrade --sql)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    try:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
        connection.commit()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
