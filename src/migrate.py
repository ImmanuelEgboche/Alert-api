"""Bring the database schema up to date with Alembic.

Runs on app startup (src/main.py) and from the command line:

    python -m src.migrate            # upgrade to the latest revision
    make migrate                     # same

Databases created before Alembic was added (v1.0.0 used create_all) have the
tables but no ``alembic_version`` table. They're detected and stamped at the
baseline revision, then upgraded normally - no manual step needed.
"""
import asyncio
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.config import get_settings

logger = structlog.get_logger()

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
BASELINE_REVISION = "0001"


class LegacySchemaError(RuntimeError):
    """The database predates the baseline and needs a manual migration first."""


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    # Keep the app's structlog setup; alembic.ini's logging is for the CLI
    cfg.attributes["configure_logger"] = False
    if database_url:
        cfg.attributes["database_url"] = database_url
    return cfg


def _schema_state(connection: Connection) -> str:
    """'empty', 'managed' (has alembic_version), or 'legacy' (tables, no version)."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return "managed"
    if "users" not in tables:
        return "empty"
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "api_key" in columns:
        raise LegacySchemaError(
            "The users table still has a plaintext api_key column. Run "
            "scripts/migrations/001_hash_api_keys.sql first (see CHANGELOG 1.0.0)."
        )
    return "legacy"


async def _detect_state(database_url: str) -> str:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_schema_state)
    finally:
        await engine.dispose()


def upgrade(database_url: str | None = None) -> None:
    """Stamp a pre-Alembic database if needed, then upgrade to head.

    Synchronous: alembic's env.py runs its own event loop. From async code,
    call it via ``asyncio.to_thread``.
    """
    url = database_url or get_settings().database_url_str
    cfg = alembic_config(url)
    state = asyncio.run(_detect_state(url))
    if state == "legacy":
        logger.info("migrations_stamping_legacy_database", revision=BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")
    logger.info("migrations_applied", previous_state=state)


async def upgrade_async(database_url: str | None = None) -> None:
    """``upgrade`` from inside a running event loop."""
    await asyncio.to_thread(upgrade, database_url)


if __name__ == "__main__":
    upgrade()
