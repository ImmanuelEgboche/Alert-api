"""Alert matching engine using Redis sorted sets for O(log n) performance."""
import asyncio
import json
import time
from datetime import datetime
from typing import Any
import structlog

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from src.models import Alert, AlertStatus, AlertCondition
from src.database import get_redis
from src.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class AlertMatcher:
    """High-performance alert matching engine.

    Uses Redis sorted sets for O(log n + k) matching:
    - Sorted set keyed by symbol
    - Score = threshold price
    - Value = alert_id

    For each price update:
    1. ZRANGEBYSCORE to find alerts with threshold <= price (for price_above)
    2. ZRANGEBYSCORE to find alerts with threshold >= price (for price_below)
    3. Fetch alert details from Redis hash
    4. Trigger matched alerts
    """

    def __init__(self) -> None:
        """Initialize alert matcher."""
        self.redis: aioredis.Redis | None = None
        self._triggered_callbacks: list[Any] = []

    async def initialize(self) -> None:
        """Initialize Redis connection."""
        self.redis = await get_redis()
        logger.info("alert_matcher_initialized")

    def add_trigger_callback(self, callback: Any) -> None:
        """Add callback for triggered alerts."""
        self._triggered_callbacks.append(callback)

    async def load_active_alerts(self, session: AsyncSession) -> int:
        """Load active alerts from database into Redis.

        Args:
            session: Database session

        Returns:
            Number of alerts loaded
        """
        if self.redis is None:
            raise RuntimeError("AlertMatcher not initialized")

        result = await session.execute(
            select(Alert).where(Alert.status == AlertStatus.ACTIVE.value)
        )
        alerts = result.scalars().all()

        loaded = 0
        for alert in alerts:
            await self._store_alert(alert)
            loaded += 1

        logger.info("loaded_active_alerts", count=loaded)
        return loaded

    async def _store_alert(self, alert: Alert) -> None:
        """Store alert in Redis data structures.

        Args:
            alert: Alert model instance
        """
        if self.redis is None:
            return

        alert_key = f"alert:{alert.id}"
        symbol_key = self._armed_key(alert.symbol, alert.condition)

        # (Re)storing an alert always resets it to armed - drop any pending re-arm entry
        await self.redis.zrem(self._rearm_key(alert.symbol, alert.condition), str(alert.id))

        # Store alert metadata in hash
        await self.redis.hset(
            alert_key,
            mapping={
                "id": alert.id,
                "user_id": alert.user_id,
                "symbol": alert.symbol,
                "condition": alert.condition,
                "threshold": alert.threshold,
                "webhook_url": alert.webhook_url,
                "is_recurring": str(alert.is_recurring),
                "created_at": alert.created_at.isoformat(),
                # Epoch seconds, "" = never expires (Redis hashes can't hold None)
                "expires_at": alert.expires_at.timestamp() if alert.expires_at else "",
            },
        )

        # Add to sorted set for efficient range queries
        await self.redis.zadd(symbol_key, {str(alert.id): alert.threshold})

        # Track user's alerts
        await self.redis.sadd(f"user:{alert.user_id}:alerts", alert.id)

        logger.debug("stored_alert", alert_id=alert.id, symbol=alert.symbol)

    async def add_alert(self, alert: Alert) -> None:
        """Add new alert to matcher.

        Args:
            alert: Alert model instance
        """
        await self._store_alert(alert)

    async def remove_alert(self, alert_id: int, symbol: str, condition: str) -> None:
        """Remove alert from matcher.

        Args:
            alert_id: Alert ID
            symbol: Trading pair symbol
            condition: Alert condition
        """
        if self.redis is None:
            return

        alert_key = f"alert:{alert_id}"

        # Get user_id before deleting
        user_id = await self.redis.hget(alert_key, "user_id")

        # Remove from all data structures (armed AND waiting-to-re-arm)
        await self.redis.delete(alert_key)
        await self.redis.zrem(self._armed_key(symbol, condition), str(alert_id))
        await self.redis.zrem(self._rearm_key(symbol, condition), str(alert_id))

        if user_id:
            await self.redis.srem(f"user:{user_id}:alerts", alert_id)

        logger.debug("removed_alert", alert_id=alert_id, symbol=symbol)

    async def match_price_update(
        self, symbol: str, price: float, timestamp: datetime, session: AsyncSession
    ) -> list[Alert]:
        """Match price update against active alerts.

        Args:
            symbol: Trading pair symbol
            price: Current price
            timestamp: Update timestamp
            session: Database session

        Returns:
            List of triggered alerts
        """
        if self.redis is None:
            return []

        triggered: list[Alert] = []

        # Re-arm recurring alerts whose price has crossed back past their re-arm level
        await self._rearm_crossed_back(symbol, price)

        # Match price_above alerts (threshold <= price)
        above_alerts = await self._match_condition(
            symbol, AlertCondition.PRICE_ABOVE.value, price, "max"
        )
        triggered.extend(above_alerts)

        # Match price_below alerts (threshold >= price)
        below_alerts = await self._match_condition(
            symbol, AlertCondition.PRICE_BELOW.value, price, "min"
        )
        triggered.extend(below_alerts)

        # Fire-time expiry check: the guarantee that an expired alert never fires,
        # regardless of when the background sweeper last ran.
        now = time.time()
        expired = [a for a in triggered if self._is_expired(a, now)]
        if expired:
            triggered = [a for a in triggered if not self._is_expired(a, now)]
            await self._expire_claimed(expired, session)

        # Process triggered alerts
        if triggered:
            await self._process_triggered_alerts(triggered, price, timestamp, session)

        return triggered

    @staticmethod
    def _is_expired(alert_data: dict[str, Any], now: float) -> bool:
        """True if the alert has an expiry and it has passed."""
        expires_at = alert_data.get("expires_at")  # missing on pre-expiry-support hashes
        return bool(expires_at) and float(expires_at) <= now

    async def _expire_claimed(self, alerts: list[dict[str, Any]], session: AsyncSession) -> None:
        """Mark claimed-but-expired alerts as EXPIRED instead of firing them."""
        ids = [int(a["id"]) for a in alerts]
        await session.execute(
            update(Alert)
            .where(Alert.id.in_(ids), Alert.status == AlertStatus.ACTIVE.value)
            .values(status=AlertStatus.EXPIRED.value)
        )
        await session.commit()
        for a in alerts:
            await self.remove_alert(int(a["id"]), a["symbol"], a["condition"])
        logger.info("alerts_expired_at_fire_time", alert_ids=ids)

    async def _match_condition(
        self, symbol: str, condition: str, price: float, match_type: str
    ) -> list[Alert]:
        """Find and CLAIM armed alerts whose condition is met at this price.

        Claiming = atomically removing the alert from the armed set. Price updates
        are processed concurrently, so two ticks can both see the same alert in
        ZRANGEBYSCORE; only the task whose ZREM returns 1 owns it and fires it.
        This guarantees each crossing fires exactly once.

        Recurring alerts are parked in the re-arm set (scored by their re-arm
        price); one-shot alerts are simply gone from the armed set.

        Args:
            symbol: Trading pair symbol
            condition: Alert condition
            price: Current price
            match_type: 'max' for price_above, 'min' for price_below

        Returns:
            List of alert data dicts this call claimed
        """
        if self.redis is None:
            return []

        armed_key = self._armed_key(symbol, condition)

        # Query sorted set based on condition
        if match_type == "max":
            # price_above: Get alerts where threshold <= price
            alert_ids = await self.redis.zrangebyscore(armed_key, "-inf", price)
        else:
            # price_below: Get alerts where threshold >= price
            alert_ids = await self.redis.zrangebyscore(armed_key, price, "+inf")

        alerts = []
        for alert_id in alert_ids:
            # Atomic claim: lose the race -> another task already fired it
            if await self.redis.zrem(armed_key, alert_id) == 0:
                continue

            alert_data = await self.redis.hgetall(f"alert:{alert_id}")
            if not alert_data:
                continue  # deleted between query and claim

            if alert_data["is_recurring"] == "True":
                rearm_at = self._rearm_price(float(alert_data["threshold"]), condition)
                await self.redis.zadd(self._rearm_key(symbol, condition), {alert_id: rearm_at})

            alerts.append(alert_data)

        return alerts  # type: ignore

    async def _rearm_crossed_back(self, symbol: str, price: float) -> int:
        """Move recurring alerts back to armed once price crosses back past re-arm level.

        price_above re-arms when price <= threshold * (1 - margin)
        price_below re-arms when price >= threshold * (1 + margin)

        The re-arm set is scored by re-arm price, so this is one O(log n + k)
        range query per condition, same as matching.

        Returns:
            Number of alerts re-armed
        """
        if self.redis is None:
            return 0

        rearmed = 0
        for condition, lo, hi in (
            (AlertCondition.PRICE_ABOVE.value, price, "+inf"),  # re-arm level >= price
            (AlertCondition.PRICE_BELOW.value, "-inf", price),  # re-arm level <= price
        ):
            rearm_key = self._rearm_key(symbol, condition)
            alert_ids = await self.redis.zrangebyscore(rearm_key, lo, hi)
            for alert_id in alert_ids:
                # Same claim pattern: only one task moves it back
                if await self.redis.zrem(rearm_key, alert_id) == 0:
                    continue
                threshold = await self.redis.hget(f"alert:{alert_id}", "threshold")
                if threshold is None:
                    continue  # alert deleted while waiting to re-arm
                await self.redis.zadd(self._armed_key(symbol, condition), {alert_id: float(threshold)})
                rearmed += 1
                logger.debug("alert_rearmed", alert_id=alert_id, symbol=symbol, price=price)

        return rearmed

    @staticmethod
    def _armed_key(symbol: str, condition: str) -> str:
        """Sorted set of alerts that can fire, scored by threshold."""
        return f"symbol:{symbol}:{condition}"

    @staticmethod
    def _rearm_key(symbol: str, condition: str) -> str:
        """Sorted set of fired recurring alerts waiting to re-arm, scored by re-arm price."""
        return f"symbol:{symbol}:{condition}:rearm"

    @staticmethod
    def _rearm_price(threshold: float, condition: str) -> float:
        """Price the market must cross back past before a recurring alert can fire again.

        The margin (hysteresis) stops a price wobbling around the threshold
        from firing on every wobble.
        """
        margin = settings.alert_rearm_margin
        if condition == AlertCondition.PRICE_ABOVE.value:
            return threshold * (1 - margin)
        return threshold * (1 + margin)

    async def _process_triggered_alerts(
        self, alerts: list[Any], price: float, timestamp: datetime, session: AsyncSession
    ) -> None:
        """Process triggered alerts: update status, remove or keep based on recurring.

        Args:
            alerts: List of triggered alert data
            price: Current price
            timestamp: Trigger timestamp
            session: Database session
        """
        for alert_data in alerts:
            alert_id = int(alert_data["id"])
            is_recurring = alert_data["is_recurring"] == "True"
            symbol = alert_data["symbol"]
            condition = alert_data["condition"]

            # Update database
            await session.execute(
                update(Alert)
                .where(Alert.id == alert_id)
                .values(
                    status=AlertStatus.ACTIVE.value if is_recurring else AlertStatus.TRIGGERED.value,
                    triggered_at=timestamp,
                )
            )

            # Already claimed out of the armed set in _match_condition. One-shot
            # alerts also drop their metadata; recurring ones wait in the re-arm set.
            if not is_recurring:
                await self.remove_alert(alert_id, symbol, condition)

            # Notify callbacks (webhook delivery)
            for callback in self._triggered_callbacks:
                try:
                    await callback(alert_data, price, timestamp)
                except Exception as e:
                    logger.error("callback_error", alert_id=alert_id, error=str(e))

            logger.info(
                "alert_triggered",
                alert_id=alert_id,
                symbol=symbol,
                price=price,
                threshold=alert_data["threshold"],
                recurring=is_recurring,
            )

        await session.commit()

    async def get_user_alert_count(self, user_id: int) -> int:
        """Get number of active alerts for a user.

        Args:
            user_id: User ID

        Returns:
            Number of active alerts
        """
        if self.redis is None:
            return 0

        count = await self.redis.scard(f"user:{user_id}:alerts")
        return count or 0

    async def get_total_alerts(self) -> int:
        """Get total number of active alerts across all users.

        Returns:
            Total active alerts
        """
        if self.redis is None:
            return 0

        # Count all alert:* keys
        keys = await self.redis.keys("alert:*")
        return len(keys)
