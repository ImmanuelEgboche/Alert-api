"""Exchange subscriptions survive a restart, and never block startup.

Uses a fake exchange connection whose connect() blocks until the test
releases it - the same as Binance being unreachable, since the real
connect() retries forever.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import dependencies as deps
from src.api.auth import display_prefix, generate_api_key, hash_api_key
from src.core import connection_manager as cm_mod
from src.core.connection_manager import ConnectionManager
from src.exchanges.base import ExchangeConnection
from src.main import app
from src.models import Alert, User


class FakeConnection(ExchangeConnection):
    """Records every SUBSCRIBE batch; connect() waits for `release`."""

    instances: list["FakeConnection"]

    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        super().__init__(ws_url, symbols)
        self.sent: list[list[str]] = []
        self.release = asyncio.Event()
        type(self).instances.append(self)

    async def connect(self) -> None:
        await self.release.wait()
        self.is_connected = True
        await self._subscribe()

    async def _subscribe(self) -> None:
        self.sent.append(list(self.symbols))

    async def _parse_message(self, message: str):
        return None


@pytest.fixture
def fake_exchange(monkeypatch):
    """Swap Binance for a fresh FakeConnection class (own instance list per test)."""
    cls = type("FakeBinance", (FakeConnection,), {"instances": []})
    monkeypatch.setattr(cm_mod, "BinanceConnection", cls)
    return cls


@pytest.fixture
async def manager(fake_exchange):
    m = ConnectionManager(MagicMock())
    yield m
    await m.disconnect_all()


async def _user(session: AsyncSession) -> tuple[User, str]:
    key = generate_api_key()
    user = User(email="r@example.com", api_key_hash=hash_api_key(key),
                api_key_prefix=display_prefix(key))
    session.add(user)
    await session.commit()
    return user, key


async def _sub(manager: ConnectionManager, symbol: str) -> None:
    """Subscribe with a timeout: a regression that awaits connect() inline
    must fail the test, not hang the suite."""
    await asyncio.wait_for(manager.subscribe_symbol(symbol), timeout=1)


def _alert(user: User, symbol: str, status: str = "active", **kw) -> Alert:
    return Alert(user_id=user.id, symbol=symbol, condition="price_above",
                 threshold=1.0, webhook_url="https://example.com/hook",
                 status=status, **kw)


# --- 1. restore on startup -------------------------------------------------

async def test_restore_subscribes_exactly_the_active_symbols(
    db_session: AsyncSession, manager, fake_exchange
):
    user, _ = await _user(db_session)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add_all([
        _alert(user, "BTCUSDT"),
        _alert(user, "BTCUSDT"),                 # duplicate symbol -> once
        _alert(user, "ETHUSDT"),
        _alert(user, "SOLUSDT", status="paused"),
        _alert(user, "XRPUSDT", status="expired", expires_at=past),
        _alert(user, "ADAUSDT", status="triggered"),
    ])
    await db_session.commit()

    restored = await asyncio.wait_for(manager.restore_subscriptions(db_session), timeout=1)

    assert restored == ["BTCUSDT", "ETHUSDT"]
    assert len(fake_exchange.instances) == 1
    conn = fake_exchange.instances[0]
    conn.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert conn.sent == [["BTCUSDT", "ETHUSDT"]]   # one batched SUBSCRIBE


async def test_restore_with_no_alerts_opens_no_connection(
    db_session: AsyncSession, manager, fake_exchange
):
    assert await manager.restore_subscriptions(db_session) == []
    assert fake_exchange.instances == []


# --- 2. never block on the exchange ----------------------------------------

async def test_subscribe_returns_while_exchange_unreachable(manager, fake_exchange):
    # connect() never completes; subscribing must still return promptly
    await asyncio.wait_for(manager.subscribe_symbols(["BTCUSDT"]), timeout=1)
    assert fake_exchange.instances[0].is_connected is False


async def test_symbols_added_while_connecting_are_included(manager, fake_exchange):
    await _sub(manager, "BTCUSDT")
    await _sub(manager, "ETHUSDT")      # connection still pending
    conn = fake_exchange.instances[0]
    assert conn.sent == []                         # nothing sent before connect

    conn.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert conn.sent == [["BTCUSDT", "ETHUSDT"]]


async def test_symbols_added_after_connecting_are_subscribed(manager, fake_exchange):
    await _sub(manager, "BTCUSDT")
    conn = fake_exchange.instances[0]
    conn.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await _sub(manager, "ETHUSDT")
    await _sub(manager, "ETHUSDT")      # already subscribed: no-op
    assert conn.sent == [["BTCUSDT"], ["BTCUSDT", "ETHUSDT"]]


async def test_shutdown_cancels_a_pending_connect(fake_exchange):
    m = ConnectionManager(MagicMock())
    await _sub(m, "BTCUSDT")
    assert len(m._connect_tasks) == 1
    await asyncio.wait_for(m.disconnect_all(), timeout=1)
    assert not m._connect_tasks


# --- 3. no duplicate connections -------------------------------------------

async def test_concurrent_subscribes_share_one_connection(manager, fake_exchange):
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
    await asyncio.gather(*(_sub(manager, s) for s in symbols))

    assert len(fake_exchange.instances) == 1
    assert sorted(fake_exchange.instances[0].symbols) == sorted(symbols)


# --- 4. resuming a paused alert --------------------------------------------

async def test_resuming_a_paused_alert_subscribes_its_symbol(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    alert = _alert(user, "SOLUSDT", status="paused")
    db_session.add(alert)
    await db_session.commit()
    cm = app.dependency_overrides[deps.get_connection_manager]()

    response = await client.patch(f"/api/v1/alerts/{alert.id}",
                                  json={"status": "active"}, headers={"X-API-Key": key})

    assert response.status_code == 200
    cm.subscribe_symbol.assert_awaited_once_with("SOLUSDT")


async def test_pausing_does_not_subscribe(client: AsyncClient, db_session: AsyncSession):
    user, key = await _user(db_session)
    alert = _alert(user, "SOLUSDT")
    db_session.add(alert)
    await db_session.commit()
    cm = app.dependency_overrides[deps.get_connection_manager]()

    response = await client.patch(f"/api/v1/alerts/{alert.id}",
                                  json={"status": "paused"}, headers={"X-API-Key": key})

    assert response.status_code == 200
    cm.subscribe_symbol.assert_not_awaited()
