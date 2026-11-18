"""Rate limiting: per-user on authenticated endpoints, per-IP on /register."""
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from src.api import rate_limit
from src.config import get_settings

settings = get_settings()


@pytest.fixture
def low_limits(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    monkeypatch.setattr(settings, "register_rate_limit_per_hour", 2)


async def register(client: AsyncClient, email: str):
    return await client.post("/api/v1/register", json={"email": email})


async def test_user_is_limited_per_minute(client: AsyncClient, low_limits):
    key = (await register(client, "a@example.com")).json()["api_key"]
    headers = {"X-API-Key": key}

    for _ in range(3):
        assert (await client.get("/api/v1/alerts", headers=headers)).status_code == 200

    r = await client.get("/api/v1/alerts", headers=headers)
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 60
    assert r.headers["X-RateLimit-Limit"] == "3"
    # Limit spans endpoints: alert creation is blocked too
    r = await client.post("/api/v1/alerts", headers=headers, json={
        "symbol": "BTCUSDT", "condition": "price_above", "threshold": 1,
        "webhook_url": "https://example.com/hook",
    })
    assert r.status_code == 429


async def test_users_have_separate_limits(client: AsyncClient, low_limits):
    a = (await register(client, "a@example.com")).json()["api_key"]
    b = (await register(client, "b@example.com")).json()["api_key"]
    for _ in range(4):
        await client.get("/api/v1/alerts", headers={"X-API-Key": a})
    assert (await client.get("/api/v1/alerts", headers={"X-API-Key": b})).status_code == 200


async def test_registration_is_limited_per_ip(client: AsyncClient, low_limits):
    assert (await register(client, "a@example.com")).status_code == 201
    assert (await register(client, "b@example.com")).status_code == 201
    r = await register(client, "c@example.com")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) <= 3600


async def test_bad_keys_do_not_consume_a_users_quota(client: AsyncClient, low_limits):
    """Only authenticated requests count, so nobody can lock a user out."""
    key = (await register(client, "a@example.com")).json()["api_key"]
    for _ in range(5):
        await client.get("/api/v1/alerts", headers={"X-API-Key": "cak_wrong"})
    assert (await client.get("/api/v1/alerts", headers={"X-API-Key": key})).status_code == 200


async def test_redis_outage_fails_open(client: AsyncClient, low_limits, monkeypatch):
    monkeypatch.setattr(rate_limit, "hit", AsyncMock(side_effect=ConnectionError("down")))
    for i in range(4):
        assert (await register(client, f"u{i}@example.com")).status_code == 201
