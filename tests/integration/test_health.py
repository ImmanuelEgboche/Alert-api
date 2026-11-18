"""/health: 200 only when this instance can actually match and fire alerts."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from src.api import dependencies as deps
from src.api import routes
from src.core.connection_manager import ConnectionManager
from src.database import get_db
from src.main import app

URL = "/api/v1/health"


def exchanges(**states: str) -> None:
    cm = app.dependency_overrides[deps.get_connection_manager]()
    cm.health_check = AsyncMock(return_value=states)


async def test_healthy_with_an_unused_exchange(client: AsyncClient):
    exchanges(binance="healthy", coinbase="idle")
    r = await client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["services"] == {"database": "healthy", "redis": "healthy",
                                "binance": "healthy", "coinbase": "idle"}


async def test_healthy_with_no_alerts_at_all(client: AsyncClient):
    exchanges(binance="idle", coinbase="idle")
    assert (await client.get(URL)).status_code == 200


async def test_503_when_a_subscribed_exchange_is_down(client: AsyncClient):
    exchanges(binance="disconnected", coinbase="idle")
    r = await client.get(URL)
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"


async def test_503_when_redis_is_down(client: AsyncClient, monkeypatch):
    async def broken_redis():
        raise ConnectionError("redis down")
    monkeypatch.setattr(routes, "get_redis", broken_redis)

    r = await client.get(URL)
    assert r.status_code == 503
    assert r.json()["services"]["redis"] == "unhealthy"


async def test_503_when_database_is_down(client: AsyncClient):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=OSError("db down"))

    async def broken_db():
        yield session
    app.dependency_overrides[get_db] = broken_db

    r = await client.get(URL)
    assert r.status_code == 503
    assert r.json()["services"]["database"] == "unhealthy"


async def test_a_hung_dependency_times_out(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(routes, "HEALTH_CHECK_TIMEOUT_SECONDS", 0.05)

    async def hung_redis():
        await asyncio.sleep(3600)
    monkeypatch.setattr(routes, "get_redis", hung_redis)

    r = await asyncio.wait_for(client.get(URL), timeout=2)
    assert r.status_code == 503


@pytest.mark.parametrize("connected, expected", [(True, "healthy"), (False, "disconnected")])
async def test_exchange_states(connected, expected):
    cm = ConnectionManager(MagicMock())
    assert await cm.health_check() == {"binance": "idle", "coinbase": "idle"}

    cm.connections["binance"]["binance"] = MagicMock(is_connected=connected)
    assert (await cm.health_check())["binance"] == expected
