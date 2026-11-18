"""Alerts that can't work are rejected, and PATCH can't corrupt alert state."""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import dependencies as deps
from src.api.auth import display_prefix, generate_api_key, hash_api_key
from src.config import get_settings
from src.exchanges.symbols import SymbolRegistry
from src.main import app
from src.models import Alert, User

NEW_ALERT = {"symbol": "BTCUSDT", "condition": "price_above", "threshold": 100000,
             "webhook_url": "https://example.com/hook"}


@pytest.fixture
async def user_key(db_session: AsyncSession) -> tuple[User, str]:
    key = generate_api_key()
    user = User(email="v@example.com", api_key_hash=hash_api_key(key),
                api_key_prefix=display_prefix(key))
    db_session.add(user)
    await db_session.commit()
    return user, key


@pytest.fixture
def matcher():
    return app.dependency_overrides[deps.get_alert_matcher]()


async def add_alert(session: AsyncSession, user: User, status: str = "active", **kw) -> Alert:
    alert = Alert(user_id=user.id, symbol="BTCUSDT", condition="price_above",
                  threshold=100000, webhook_url="https://example.com/hook",
                  status=status, **kw)
    session.add(alert)
    await session.commit()
    return alert


def future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# --- 1. PATCH webhook_url --------------------------------------------------------

async def test_patch_webhook_url(client: AsyncClient, db_session, user_key):
    user, key = user_key
    alert = await add_alert(db_session, user)

    r = await client.patch(f"/api/v1/alerts/{alert.id}", headers={"X-API-Key": key},
                           json={"webhook_url": "https://hooks.example.org/new"})

    assert r.status_code == 200
    assert r.json()["webhook_url"] == "https://hooks.example.org/new"
    await db_session.refresh(alert)
    assert alert.webhook_url == "https://hooks.example.org/new"


# --- 2. deleted alerts stay deleted -----------------------------------------------

async def test_deleted_alert_is_gone_everywhere(client: AsyncClient, db_session, user_key, matcher):
    user, key = user_key
    alert = await add_alert(db_session, user)
    h = {"X-API-Key": key}
    assert (await client.delete(f"/api/v1/alerts/{alert.id}", headers=h)).status_code == 204
    matcher.add_alert.reset_mock()

    assert (await client.get(f"/api/v1/alerts/{alert.id}", headers=h)).status_code == 404
    assert (await client.get(f"/api/v1/alerts/{alert.id}/logs", headers=h)).status_code == 404
    assert (await client.delete(f"/api/v1/alerts/{alert.id}", headers=h)).status_code == 404
    r = await client.patch(f"/api/v1/alerts/{alert.id}", headers=h, json={"status": "active"})
    assert r.status_code == 404
    matcher.add_alert.assert_not_awaited()          # not resurrected into the matcher
    assert (await client.get("/api/v1/alerts", headers=h)).json() == []


async def test_list_status_filter_is_validated(client: AsyncClient, user_key):
    _, key = user_key
    for bad in ("deleted", "banana"):
        r = await client.get(f"/api/v1/alerts?status={bad}", headers={"X-API-Key": key})
        assert r.status_code == 422


# --- 3. status transitions ----------------------------------------------------------

@pytest.mark.parametrize("bad", ["banana", "triggered", "expired", "deleted", ""])
async def test_patch_rejects_non_user_statuses(client: AsyncClient, db_session, user_key, bad):
    user, key = user_key
    alert = await add_alert(db_session, user)
    r = await client.patch(f"/api/v1/alerts/{alert.id}", headers={"X-API-Key": key},
                           json={"status": bad})
    assert r.status_code == 422


async def test_pause_and_resume(client: AsyncClient, db_session, user_key, matcher):
    user, key = user_key
    alert = await add_alert(db_session, user)
    url, h = f"/api/v1/alerts/{alert.id}", {"X-API-Key": key}

    r = await client.patch(url, headers=h, json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    matcher.add_alert.assert_not_awaited()

    r = await client.patch(url, headers=h, json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    matcher.add_alert.assert_awaited_once()


async def test_triggered_one_shot_can_be_rearmed(client: AsyncClient, db_session, user_key):
    user, key = user_key
    alert = await add_alert(db_session, user, status="triggered")
    r = await client.patch(f"/api/v1/alerts/{alert.id}", headers={"X-API-Key": key},
                           json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"


async def test_expired_alert_needs_a_new_expiry_to_reactivate(
    client: AsyncClient, db_session, user_key, matcher
):
    user, key = user_key
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    alert = await add_alert(db_session, user, status="expired", expires_at=past)
    url, h = f"/api/v1/alerts/{alert.id}", {"X-API-Key": key}

    r = await client.patch(url, headers=h, json={"status": "active"})
    assert r.status_code == 409
    matcher.add_alert.assert_not_awaited()

    r = await client.patch(url, headers=h, json={"status": "active", "expires_at": future()})
    assert r.status_code == 200 and r.json()["status"] == "active"


# --- 4. alert limit on reactivation ---------------------------------------------

async def test_resume_respects_the_alert_limit(client: AsyncClient, db_session, user_key, matcher):
    user, key = user_key
    alert = await add_alert(db_session, user, status="paused")
    matcher.get_user_alert_count.return_value = get_settings().max_alerts_per_user
    url, h = f"/api/v1/alerts/{alert.id}", {"X-API-Key": key}

    r = await client.patch(url, headers=h, json={"status": "active"})
    assert r.status_code == 400
    await db_session.refresh(alert)
    assert alert.status == "paused"

    # editing an already-active alert at the limit is fine (it's not a new one)
    active = await add_alert(db_session, user)
    r = await client.patch(f"/api/v1/alerts/{active.id}", headers=h,
                           json={"status": "active", "threshold": 1})
    assert r.status_code == 200


# --- 5 & 6. alerts that could never fire ----------------------------------------

async def test_price_change_percent_rejected(client: AsyncClient, user_key):
    _, key = user_key
    r = await client.post("/api/v1/alerts", headers={"X-API-Key": key},
                          json={**NEW_ALERT, "condition": "price_change_percent"})
    assert r.status_code == 422
    assert "not supported yet" in r.text


@pytest.mark.parametrize("symbol", ["NOTACOIN", "BTCUSD"])
async def test_unknown_symbol_rejected(client: AsyncClient, user_key, matcher, symbol):
    _, key = user_key
    r = await client.post("/api/v1/alerts", headers={"X-API-Key": key},
                          json={**NEW_ALERT, "symbol": symbol})
    assert r.status_code == 422
    assert "not a trading symbol" in r.text
    matcher.add_alert.assert_not_awaited()


@pytest.mark.parametrize("symbol", ["BTC-USDT", "BTC/USDT", "BTC USDT", "ＢＴＣUSDT"])
async def test_malformed_symbol_rejected(client: AsyncClient, user_key, symbol):
    _, key = user_key
    r = await client.post("/api/v1/alerts", headers={"X-API-Key": key},
                          json={**NEW_ALERT, "symbol": symbol})
    assert r.status_code == 422


async def test_symbol_is_normalised(client: AsyncClient, user_key):
    _, key = user_key
    r = await client.post("/api/v1/alerts", headers={"X-API-Key": key},
                          json={**NEW_ALERT, "symbol": " ethusdt "})
    assert r.status_code == 201
    assert r.json()["symbol"] == "ETHUSDT"


async def test_503_until_symbol_list_loads(client: AsyncClient, user_key):
    _, key = user_key
    app.dependency_overrides[deps.get_symbol_registry] = lambda: SymbolRegistry()  # not loaded
    r = await client.post("/api/v1/alerts", headers={"X-API-Key": key}, json=NEW_ALERT)
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "30"
