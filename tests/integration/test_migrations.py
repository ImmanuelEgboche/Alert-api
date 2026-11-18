"""Alembic migrations: they must build exactly the schema the models describe,
and pre-Alembic (v1.0.0) databases must be adopted without changes.

Sync tests on purpose: alembic's env.py runs its own event loop.
"""
import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src import migrate
from src.models import Base
from tests.conftest import TEST_DATABASE_URL


def run(coro_fn):
    """Run fn(conn) on a fresh connection to the test database."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(coro_fn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def drop_everything(conn) -> None:
    Base.metadata.drop_all(conn)
    conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def head_revision() -> str:
    return ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()


def current_revision(conn) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


@pytest.fixture
def clean_db():
    run(drop_everything)
    yield
    run(drop_everything)


def test_migrations_match_models(clean_db):
    migrate.upgrade(TEST_DATABASE_URL)
    diff = run(lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata))
    assert diff == [], f"Models and migrations differ - run `make migration`: {diff}"
    assert run(current_revision) == head_revision()


def test_upgrade_is_idempotent(clean_db):
    migrate.upgrade(TEST_DATABASE_URL)
    migrate.upgrade(TEST_DATABASE_URL)
    assert run(current_revision) == head_revision()


def test_pre_alembic_database_is_stamped_then_upgraded(clean_db):
    """A v1.0.0 database (create_all, no alembic_version) is adopted in place."""
    from alembic import command

    # Build the exact v1.0.0 schema, then forget it was built by Alembic
    command.upgrade(migrate.alembic_config(TEST_DATABASE_URL), migrate.BASELINE_REVISION)
    run(lambda conn: conn.execute(text("DROP TABLE alembic_version")))
    assert asyncio.run(migrate._detect_state(TEST_DATABASE_URL)) == "legacy"

    migrate.upgrade(TEST_DATABASE_URL)

    assert run(current_revision) == head_revision()
    diff = run(lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata))
    assert diff == []


def test_plaintext_key_database_is_refused(clean_db):
    run(lambda conn: conn.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY, api_key TEXT)")))
    with pytest.raises(migrate.LegacySchemaError, match="001_hash_api_keys"):
        migrate.upgrade(TEST_DATABASE_URL)
