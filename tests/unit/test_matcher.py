"""Unit tests for alert matching engine."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.config import get_settings
from src.core.matcher import AlertMatcher
from src.models import Alert, AlertStatus, AlertCondition


@pytest.mark.asyncio
async def test_alert_matcher_initialization():
    """Test alert matcher initialization."""
    matcher = AlertMatcher()
    await matcher.initialize()
    assert matcher.redis is not None


@pytest.mark.asyncio
async def test_store_and_retrieve_alert():
    """Test storing and retrieving alerts."""
    matcher = AlertMatcher()
    await matcher.initialize()

    # Create test alert
    alert = Alert(
        id=1,
        user_id=1,
        symbol="BTCUSDT",
        condition=AlertCondition.PRICE_ABOVE.value,
        threshold=50000.0,
        webhook_url="https://example.com/webhook",
        is_recurring=False,
        status=AlertStatus.ACTIVE.value,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    await matcher.add_alert(alert)

    # Verify stored
    user_count = await matcher.get_user_alert_count(1)
    assert user_count == 1


@pytest.mark.asyncio
async def test_price_above_matching():
    """Test price_above alert matching."""
    matcher = AlertMatcher()
    await matcher.initialize()

    # Create alert for price above 50000
    alert = Alert(
        id=1,
        user_id=1,
        symbol="BTCUSDT",
        condition=AlertCondition.PRICE_ABOVE.value,
        threshold=50000.0,
        webhook_url="https://example.com/webhook",
        is_recurring=False,
        status=AlertStatus.ACTIVE.value,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    await matcher.add_alert(alert)

    # Test with price below threshold - should not trigger
    triggered = await matcher._match_condition("BTCUSDT", "price_above", 49999.0, "max")
    assert len(triggered) == 0

    # Test with price above threshold - should trigger
    triggered = await matcher._match_condition("BTCUSDT", "price_above", 50001.0, "max")
    assert len(triggered) == 1


@pytest.mark.asyncio
async def test_remove_alert():
    """Test removing alerts."""
    matcher = AlertMatcher()
    await matcher.initialize()

    alert = Alert(
        id=1,
        user_id=1,
        symbol="BTCUSDT",
        condition=AlertCondition.PRICE_ABOVE.value,
        threshold=50000.0,
        webhook_url="https://example.com/webhook",
        is_recurring=False,
        status=AlertStatus.ACTIVE.value,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    await matcher.add_alert(alert)
    await matcher.remove_alert(1, "BTCUSDT", "price_above")

    # Verify removed
    user_count = await matcher.get_user_alert_count(1)
    assert user_count == 0


# ---------------------------------------------------------------------------
# Recurring alerts: fire once per crossing, re-arm after crossing back
# ---------------------------------------------------------------------------
MARGIN = get_settings().alert_rearm_margin


@pytest.fixture
async def rig():
    """Matcher + unique symbol + fired-alert recorder; cleans its Redis keys after."""
    matcher = AlertMatcher()
    await matcher.initialize()
    symbol = f"TEST{uuid.uuid4().hex[:8].upper()}"
    base_id = 9_000_000 + uuid.uuid4().int % 1_000_000
    fired: list[tuple[int, float]] = []

    async def record(alert_data, price, ts):
        fired.append((int(alert_data["id"]), price))

    matcher.add_trigger_callback(record)
    session = AsyncMock()  # DB writes are not under test here

    async def tick(price: float):
        await matcher.match_price_update(symbol, price, datetime.now(), session)

    async def add(offset=0, condition="price_above", threshold=50000.0, recurring=True,
                  expires_at=None):
        alert = Alert(
            id=base_id + offset, user_id=base_id, symbol=symbol, condition=condition,
            threshold=threshold, webhook_url="https://example.com/hook",
            is_recurring=recurring, status=AlertStatus.ACTIVE.value, created_at=datetime.now(),
            expires_at=expires_at,
        )
        await matcher.add_alert(alert)
        return alert

    yield matcher, symbol, fired, tick, add

    keys = await matcher.redis.keys(f"symbol:{symbol}:*")
    keys += [f"alert:{base_id + i}" for i in range(10)] + [f"user:{base_id}:alerts"]
    await matcher.redis.delete(*keys)


async def test_recurring_fires_once_while_price_stays_above(rig):
    _, _, fired, tick, add = rig
    await add()
    for p in [50001, 50100, 50500, 51000, 50002]:  # 5 trades all above threshold
        await tick(p)
    assert len(fired) == 1


async def test_recurring_rearms_after_crossing_back_past_margin(rig):
    _, _, fired, tick, add = rig
    await add(threshold=50000.0)
    await tick(50100)                          # fire #1
    await tick(50000 * (1 - MARGIN) - 1)       # crosses back past margin -> re-arm
    await tick(50100)                          # fire #2
    assert len(fired) == 2


async def test_wobble_inside_margin_does_not_rearm(rig):
    _, _, fired, tick, add = rig
    await add(threshold=50000.0)
    for p in [50001, 49999, 50001, 49999, 50001]:  # wobble within 0.1% of threshold
        await tick(p)
    assert len(fired) == 1


async def test_price_below_recurring_symmetric(rig):
    _, _, fired, tick, add = rig
    await add(condition="price_below", threshold=40000.0)
    await tick(39900)                          # fire #1
    await tick(39800)                          # still below -> no fire
    await tick(40000 * (1 + MARGIN) + 1)       # back above margin -> re-arm
    await tick(39900)                          # fire #2
    assert len(fired) == 2


async def test_one_shot_never_fires_again(rig):
    _, _, fired, tick, add = rig
    await add(recurring=False)
    await tick(50100)
    await tick(40000)                          # crosses back
    await tick(50100)
    assert len(fired) == 1


@pytest.mark.parametrize("recurring", [True, False])
async def test_concurrent_burst_fires_exactly_once(rig, recurring):
    """20 trades processed concurrently must not double-fire (atomic ZREM claim)."""
    _, _, fired, tick, add = rig
    await add(recurring=recurring)
    await asyncio.gather(*(tick(50100 + i) for i in range(20)))
    assert len(fired) == 1


async def test_remove_clears_rearm_set(rig):
    matcher, symbol, fired, tick, add = rig
    alert = await add()
    await tick(50100)                          # fired, now waiting in re-arm set
    await matcher.remove_alert(alert.id, symbol, "price_above")
    assert await matcher.redis.zcard(f"symbol:{symbol}:price_above:rearm") == 0
    await tick(40000)
    await tick(50100)
    assert len(fired) == 1                     # deleted alert never comes back


async def test_restore_resets_to_armed(rig):
    """Re-storing (e.g. PATCH or app restart) puts the alert back in armed only."""
    matcher, symbol, fired, tick, add = rig
    await add()
    await tick(50100)                          # fired -> re-arm set
    await add()                                # store again
    assert await matcher.redis.zcard(f"symbol:{symbol}:price_above:rearm") == 0
    assert await matcher.redis.zcard(f"symbol:{symbol}:price_above") == 1


# ---------------------------------------------------------------------------
# Expiry: fire-time check
# ---------------------------------------------------------------------------


async def test_expired_alert_does_not_fire_and_is_removed(rig):
    matcher, symbol, fired, tick, add = rig
    alert = await add(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await tick(50100)                          # condition met, but expired
    assert fired == []
    assert await matcher.redis.exists(f"alert:{alert.id}") == 0
    assert await matcher.get_user_alert_count(alert.user_id) == 0  # frees the user's quota


async def test_alert_with_future_expiry_still_fires(rig):
    _, _, fired, tick, add = rig
    await add(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    await tick(50100)
    assert len(fired) == 1


async def test_recurring_alert_expiring_while_waiting_to_rearm(rig):
    matcher, symbol, fired, tick, add = rig
    alert = await add(expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.3))
    await tick(50100)                          # fire #1, parked in re-arm set
    await asyncio.sleep(0.35)                  # expires meanwhile
    await tick(40000)                          # re-arms
    await tick(50100)                          # would fire #2 - must not
    assert len(fired) == 1
    assert await matcher.redis.exists(f"alert:{alert.id}") == 0
