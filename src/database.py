"""Database connection and session management."""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool
import redis.asyncio as aioredis

from src.config import get_settings

settings = get_settings()

# PostgreSQL async engine
engine = create_async_engine(
    settings.database_url_str,
    echo=settings.debug,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    poolclass=NullPool if settings.environment == "test" else None,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Redis connection pool. Blocking: when every connection is busy a caller
# waits (up to the timeout) instead of failing with "Too many connections".
redis_pool: aioredis.BlockingConnectionPool | None = None
REDIS_POOL_TIMEOUT_SECONDS = 5


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session dependency."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_redis() -> aioredis.Redis:
    """Get Redis connection."""
    global redis_pool
    if redis_pool is None:
        redis_pool = aioredis.BlockingConnectionPool.from_url(
            settings.redis_url_str,
            max_connections=settings.redis_max_connections,
            timeout=REDIS_POOL_TIMEOUT_SECONDS,
            decode_responses=True,
        )
    return aioredis.Redis(connection_pool=redis_pool)


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
    if redis_pool is not None:
        await redis_pool.disconnect()
