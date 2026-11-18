"""Integration tests for API endpoints."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User, Alert
from sqlalchemy import select

from src.api.auth import display_prefix, generate_api_key, hash_api_key


@pytest.mark.asyncio
async def test_register_user(client: AsyncClient, db_session: AsyncSession):
    """Test user registration."""
    response = await client.post(
        "/api/v1/register",
        json={"email": "test@example.com"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "test@example.com"
    assert "api_key" in data


@pytest.mark.asyncio
async def test_register_duplicate_email(client: AsyncClient, db_session: AsyncSession):
    """Test registering with duplicate email."""
    await client.post("/api/v1/register", json={"email": "test@example.com"})
    response = await client.post("/api/v1/register", json={"email": "test@example.com"})

    assert response.status_code == 400
    assert "already registered" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_alert(client: AsyncClient, db_session: AsyncSession):
    """Test creating an alert."""
    # Create user
    api_key = generate_api_key()
    user = User(
        email="test@example.com",
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=display_prefix(api_key),
    )
    db_session.add(user)
    await db_session.commit()

    # Create alert
    response = await client.post(
        "/api/v1/alerts",
        headers={"X-API-Key": api_key},
        json={
            "symbol": "BTCUSDT",
            "condition": "price_above",
            "threshold": 50000.0,
            "webhook_url": "https://example.com/webhook",
            "is_recurring": False,
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["symbol"] == "BTCUSDT"
    assert data["threshold"] == 50000.0


@pytest.mark.asyncio
async def test_list_alerts(client: AsyncClient, db_session: AsyncSession):
    """Test listing user alerts."""
    # Create user
    api_key = generate_api_key()
    user = User(
        email="test@example.com",
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=display_prefix(api_key),
    )
    db_session.add(user)
    await db_session.commit()

    # Create alerts
    for i in range(3):
        alert = Alert(
            user_id=user.id,
            symbol="BTCUSDT",
            condition="price_above",
            threshold=50000.0 + i * 1000,
            webhook_url="https://example.com/webhook",
            status="active",
        )
        db_session.add(alert)
    await db_session.commit()

    # List alerts
    response = await client.get(
        "/api/v1/alerts",
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_unauthorized_access(client: AsyncClient):
    """Test unauthorized API access."""
    response = await client.get("/api/v1/alerts")
    # FastAPI returns 422 when required header is missing (validation error)
    # or 401 if header is provided but invalid
    assert response.status_code in [401, 422]


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """Test health check endpoint."""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "services" in data


# ---------------------------------------------------------------------------
# API key storage: plaintext never persisted, hash lookup works
# ---------------------------------------------------------------------------


async def test_register_returns_key_once_and_stores_only_hash(
    client: AsyncClient, db_session: AsyncSession
):
    response = await client.post("/api/v1/register", json={"email": "k@example.com"})
    assert response.status_code == 201
    api_key = response.json()["api_key"]
    assert api_key.startswith("cak_") and len(api_key) == 4 + 64

    user = (await db_session.execute(select(User))).scalar_one()
    assert user.api_key_hash == hash_api_key(api_key)
    assert user.api_key_prefix == api_key[:12]
    assert not hasattr(User, "api_key")  # no plaintext column on the model
    assert api_key not in user.api_key_hash


async def test_registered_key_authenticates(client: AsyncClient):
    api_key = (
        await client.post("/api/v1/register", json={"email": "a@example.com"})
    ).json()["api_key"]
    response = await client.get("/api/v1/alerts", headers={"X-API-Key": api_key})
    assert response.status_code == 200


async def test_wrong_key_with_matching_prefix_rejected(client: AsyncClient):
    """Old auth matched on the first 8 chars; a key sharing the prefix must still fail."""
    api_key = (
        await client.post("/api/v1/register", json={"email": "p@example.com"})
    ).json()["api_key"]
    forged = api_key[:-1] + ("0" if api_key[-1] != "0" else "1")
    response = await client.get("/api/v1/alerts", headers={"X-API-Key": forged})
    assert response.status_code == 401


async def test_disabled_user_forbidden(client: AsyncClient, db_session: AsyncSession):
    api_key = generate_api_key()
    db_session.add(User(
        email="off@example.com", api_key_hash=hash_api_key(api_key),
        api_key_prefix=display_prefix(api_key), is_active=False,
    ))
    await db_session.commit()
    response = await client.get("/api/v1/alerts", headers={"X-API-Key": api_key})
    assert response.status_code == 403
