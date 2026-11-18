"""Pytest configuration and fixtures.

Each test runs in its own event loop (pytest-asyncio's default). Async
connections belong to the loop that opened them, so nothing loop-bound may
outlive a test: the test DB engine uses NullPool, and the app's global Redis
pool is reset after every test.
"""
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from src import database
from src.main import app
from src.database import get_db
from src.models import Base
from src.config import get_settings
from src.api import dependencies as deps
from src.api import rate_limit
from src.exchanges.symbols import SymbolRegistry

settings = get_settings()

# Test database URL
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/cryptoalert_test"

# NullPool: open a fresh connection per use instead of pooling, so no
# connection created in one test's loop is reused in the next test's loop.
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

# Symbols the test client treats as tradable on Binance (no network in tests)
TEST_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

TestSessionLocal = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_redis_pool() -> AsyncGenerator[None, None]:
    """Drop the app's global Redis pool after each test (it's bound to that test's loop)."""
    yield
    if database.redis_pool is not None:
        await database.redis_pool.disconnect()
        database.redis_pool = None


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    # Create tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create session
    async with TestSessionLocal() as session:
        yield session

    # Drop tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create test HTTP client."""

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    # Create mock alert matcher
    mock_matcher = MagicMock()
    mock_matcher.get_user_alert_count = AsyncMock(return_value=0)
    mock_matcher.add_alert = AsyncMock()
    mock_matcher.remove_alert = AsyncMock()
    mock_matcher.get_total_alerts = AsyncMock(return_value=0)

    # Create mock connection manager
    mock_connection_manager = MagicMock()
    mock_connection_manager.subscribe_symbol = AsyncMock()
    mock_connection_manager.health_check = AsyncMock(return_value={"binance": "healthy", "coinbase": "idle"})
    mock_connection_manager.get_active_connections = AsyncMock(return_value={"binance": 1, "coinbase": 0})

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[deps.get_alert_matcher] = lambda: mock_matcher
    app.dependency_overrides[deps.get_connection_manager] = lambda: mock_connection_manager
    registry = SymbolRegistry(symbols=TEST_SYMBOLS)
    app.dependency_overrides[deps.get_symbol_registry] = lambda: registry

    await clear_rate_limits()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await clear_rate_limits()


async def clear_rate_limits() -> None:
    """Rate-limit counters live in Redis for up to an hour; without this, one
    test's requests would count against the next (user ids restart at 1)."""
    redis = await database.get_redis()
    keys = [k async for k in redis.scan_iter(match=f"{rate_limit.KEY_PREFIX}*")]
    if keys:
        await redis.delete(*keys)
