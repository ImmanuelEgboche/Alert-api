"""Alert expiry: bulk sweeper that retires alerts past their expires_at.

Two layers enforce expiry:
1. Fire-time check in AlertMatcher.match_price_update - the correctness
   guarantee: an expired alert never fires, however stale the sweep is.
2. This sweeper - housekeeping: alerts on quiet symbols may never see a
   price tick, so without it they'd sit in Redis forever and keep counting
   toward the user's max-alerts limit.
"""
import asyncio

import structlog
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.matcher import AlertMatcher
from src.database import AsyncSessionLocal
from src.models import Alert, AlertStatus

logger = structlog.get_logger()

# Statuses that can still expire (paused alerts shouldn't outlive their expiry either)
EXPIRABLE = (AlertStatus.ACTIVE.value, AlertStatus.PAUSED.value)


async def expire_due_alerts(session: AsyncSession, matcher: AlertMatcher) -> int:
    """Mark every due alert EXPIRED and remove it from the matcher.

    One UPDATE ... RETURNING: atomic, so concurrent sweeps (e.g. several app
    instances) can't double-process, and no separate SELECT round-trip.

    Returns:
        Number of alerts expired
    """
    result = await session.execute(
        update(Alert)
        .where(
            Alert.status.in_(EXPIRABLE),
            Alert.expires_at.is_not(None),
            Alert.expires_at <= func.now(),
        )
        .values(status=AlertStatus.EXPIRED.value)
        .returning(Alert.id, Alert.symbol, Alert.condition)
    )
    expired = result.all()
    await session.commit()

    for alert_id, symbol, condition in expired:
        await matcher.remove_alert(alert_id, symbol, condition)

    if expired:
        logger.info("alerts_expired_by_sweeper", count=len(expired))
    return len(expired)


async def run_expiry_sweeper(matcher: AlertMatcher, interval_seconds: float) -> None:
    """Run expire_due_alerts forever, every interval_seconds. Cancel to stop."""
    logger.info("expiry_sweeper_started", interval_seconds=interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with AsyncSessionLocal() as session:
                await expire_due_alerts(session, matcher)
        except Exception as e:
            # A failed sweep (e.g. DB blip) must not kill the loop
            logger.error("expiry_sweep_failed", error=str(e))
