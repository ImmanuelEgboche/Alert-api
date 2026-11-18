"""Integration tests for alert expiry: sweeper SQL against real Postgres + API validation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import display_prefix, generate_api_key, hash_api_key
from src.core.expiry import expire_due_alerts
from src.models import Alert, AlertStatus, User


async def make_user(db_session: AsyncSession) -> tuple[User, str]:
    api_key = generate_api_key()
    user = User(email="exp@example.com", api_key_hash=hash_api_key(api_key),
                api_key_prefix=display_prefix(api_key))
    db_session.add(user)
    await db_session.commit()
    return user, api_key


def make_alert(user: User, status: str, expires_at: datetime | None) -> Alert:
    return Alert(user_id=user.id, symbol="BTCUSDT", condition="price_above", threshold=50000.0,
                 webhook_url="https://example.com/hook", status=status, expires_at=expires_at)


async def test_sweeper_expires_only_due_alerts(db_session: AsyncSession):
    user, _ = await make_user(db_session)
    now = datetime.now(timezone.utc)
    alerts = {
        "due_active": make_alert(user, "active", now - timedelta(minutes=1)),
        "due_paused": make_alert(user, "paused", now - timedelta(minutes=1)),
        "future": make_alert(user, "active", now + timedelta(hours=1)),
        "no_expiry": make_alert(user, "active", None),
        "due_but_triggered": make_alert(user, "triggered", now - timedelta(minutes=1)),
    }
    db_session.add_all(alerts.values())
    await db_session.commit()

    matcher = AsyncMock()
    count = await expire_due_alerts(db_session, matcher)

    assert count == 2
    for a in alerts.values():
        await db_session.refresh(a)
    assert alerts["due_active"].status == AlertStatus.EXPIRED.value
    assert alerts["due_paused"].status == AlertStatus.EXPIRED.value
    assert alerts["future"].status == "active"
    assert alerts["no_expiry"].status == "active"
    assert alerts["due_but_triggered"].status == "triggered"  # terminal states untouched
    removed = {c.args[0] for c in matcher.remove_alert.call_args_list}
    assert removed == {alerts["due_active"].id, alerts["due_paused"].id}

    # Idempotent: a second sweep finds nothing
    assert await expire_due_alerts(db_session, matcher) == 0


async def test_create_alert_rejects_past_expiry(client: AsyncClient, db_session: AsyncSession):
    _, api_key = await make_user(db_session)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    response = await client.post(
        "/api/v1/alerts", headers={"X-API-Key": api_key},
        json={"symbol": "BTCUSDT", "condition": "price_above", "threshold": 50000.0,
              "webhook_url": "https://example.com/hook", "expires_at": past},
    )
    assert response.status_code == 422
    assert "future" in response.text


async def test_create_alert_naive_expiry_treated_as_utc(client: AsyncClient, db_session: AsyncSession):
    _, api_key = await make_user(db_session)
    naive = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    response = await client.post(
        "/api/v1/alerts", headers={"X-API-Key": api_key},
        json={"symbol": "BTCUSDT", "condition": "price_above", "threshold": 50000.0,
              "webhook_url": "https://example.com/hook", "expires_at": naive},
    )
    assert response.status_code == 201
    stored = (await db_session.execute(select(Alert))).scalar_one().expires_at
    assert abs(stored - (datetime.now(timezone.utc) + timedelta(hours=2))) < timedelta(seconds=5)
