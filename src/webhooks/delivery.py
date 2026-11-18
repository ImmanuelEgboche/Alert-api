"""Webhook delivery with retry logic and dead letter queue."""
import asyncio
import hashlib
import hmac
import random
import time
from datetime import datetime
from typing import Any
import structlog

import httpx

from src.config import get_settings
from src.database import AsyncSessionLocal
from sqlalchemy import select

from src.models import User, WebhookLog, WebhookStatus
from src.schemas import WebhookPayload
from src.webhooks.url_safety import UnsafeWebhookURL, create_safe_transport

logger = structlog.get_logger()
settings = get_settings()

# Statuses worth retrying: the receiver may succeed later. Everything else in
# 4xx (404, 401, 410, ...) will fail identically every time, so fail fast.
RETRYABLE_STATUS_CODES = {408, 425, 429}


def is_retryable_status(status_code: int) -> bool:
    """True for 5xx and the transient 4xx codes."""
    return status_code >= 500 or status_code in RETRYABLE_STATUS_CODES


class WebhookDelivery:
    """Reliable webhook delivery system with retries and monitoring.

    Design:
    - A pool of N workers pulls from one queue, so a slow or dead endpoint only
      ties up one worker instead of stalling every delivery.
    - Retries are scheduled as timers (_requeue_after) - workers never sleep on
      backoff, and never block putting items back on their own queue.
    """

    def __init__(self) -> None:
        """Initialize webhook delivery system."""
        self.client: httpx.AsyncClient | None = None
        self.delivery_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=settings.max_webhook_queue_size
        )
        self._worker_tasks: list[asyncio.Task[Any]] = []
        # Strong refs to pending retry timers (asyncio holds only weak refs)
        self._retry_tasks: set[asyncio.Task[Any]] = set()
        self._is_running = False

    async def initialize(self) -> None:
        """Initialize HTTP client and start the worker pool."""
        limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
        # SSRF guard: every connection is vetted at connect time (see url_safety).
        # follow_redirects stays False: a redirect could point anywhere.
        transport = (
            httpx.AsyncHTTPTransport(limits=limits)
            if settings.webhook_allow_private_urls
            else create_safe_transport(limits)
        )
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.webhook_timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )
        self._is_running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker(i)) for i in range(settings.webhook_workers)
        ]
        logger.info("webhook_delivery_initialized", workers=settings.webhook_workers)

    async def shutdown(self) -> None:
        """Shutdown: stop workers and cancel pending retry timers."""
        self._is_running = False
        tasks = [*self._worker_tasks, *self._retry_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._retry_tasks:
            logger.warning("webhook_retries_dropped_on_shutdown", count=len(self._retry_tasks))
        self._worker_tasks.clear()
        self._retry_tasks.clear()
        if self.client:
            await self.client.aclose()
        logger.info("webhook_delivery_shutdown")

    @staticmethod
    def _generate_signature(payload: str, secret: str) -> str:
        """HMAC-SHA256 of the payload with the alert owner's webhook secret.

        Args:
            payload: JSON payload as string
            secret: The user's webhook_secret

        Returns:
            Hex-encoded signature
        """
        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    async def _get_signing_secret(self, user_id: int) -> str | None:
        """The alert owner's current webhook secret; None if the user is gone or
        disabled. Read on every attempt, so a rotated secret applies to retries."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User.webhook_secret).where(User.id == user_id, User.is_active.is_(True))
            )
            return result.scalar_one_or_none()

    async def queue_webhook(
        self,
        alert_data: dict[str, Any],
        current_price: float,
        timestamp: datetime,
    ) -> None:
        """Queue webhook for delivery.

        Signature matches AlertMatcher's trigger callback: (alert_data, price, timestamp).
        No DB session is passed in - queued items can outlive any caller's session,
        so _log_delivery opens its own short-lived session instead.

        Args:
            alert_data: Alert data from matcher
            current_price: Current price that triggered alert
            timestamp: Trigger timestamp
        """
        webhook_data = {
            "alert_id": int(alert_data["id"]),
            "alert_data": alert_data,
            "current_price": current_price,
            "timestamp": timestamp,
            "retry_count": 0,
        }

        try:
            self.delivery_queue.put_nowait(webhook_data)
            logger.debug("webhook_queued", alert_id=alert_data["id"])
        except asyncio.QueueFull:
            logger.error("webhook_queue_full", alert_id=alert_data["id"])
            # Log failed delivery
            await self._log_delivery(
                int(alert_data["id"]),
                WebhookStatus.FAILED.value,
                error_message="Queue full",
            )

    async def _worker(self, worker_id: int = 0) -> None:
        """Background worker to process webhook deliveries (one of a pool)."""
        logger.debug("webhook_worker_started", worker_id=worker_id)

        while self._is_running:
            try:
                # Get webhook from queue with timeout
                webhook_data = await asyncio.wait_for(
                    self.delivery_queue.get(), timeout=1.0
                )

                # Deliver webhook
                await self._deliver_webhook(webhook_data)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("webhook_worker_error", error=str(e))
                await asyncio.sleep(1)

    async def _deliver_webhook(self, webhook_data: dict[str, Any]) -> None:
        """Deliver webhook with retry logic.

        Args:
            webhook_data: Webhook delivery data
        """
        alert_id = webhook_data["alert_id"]
        alert_data = webhook_data["alert_data"]
        current_price = webhook_data["current_price"]
        timestamp = webhook_data["timestamp"]
        retry_count = webhook_data["retry_count"]

        webhook_url = alert_data["webhook_url"]

        try:
            secret = await self._get_signing_secret(int(alert_data["user_id"]))
        except Exception as e:
            # DB hiccup: the receiver is fine, so try again later
            await self._handle_delivery_failure(
                webhook_data, f"Signing secret unavailable: {e}", None, 0.0, retryable=True
            )
            return
        if secret is None:
            await self._handle_delivery_failure(
                webhook_data, "Account disabled or deleted", None, 0.0, retryable=False
            )
            return

        # Build payload
        payload = WebhookPayload(
            alert_id=alert_id,
            symbol=alert_data["symbol"],
            condition=alert_data["condition"],
            threshold=float(alert_data["threshold"]),
            current_price=current_price,
            triggered_at=timestamp,
            signature="",  # Will be set after serialization
        )

        payload_json = payload.model_dump_json(exclude={"signature"})
        signature = self._generate_signature(payload_json, secret)

        # Add signature
        payload.signature = signature
        final_payload = payload.model_dump_json()

        # Deliver with timing
        start_time = time.perf_counter()
        try:
            response = await self._send_webhook(webhook_url, final_payload, signature)
            response_time_ms = (time.perf_counter() - start_time) * 1000

            if response.is_success:
                # Success
                await self._log_delivery(
                    alert_id,
                    WebhookStatus.DELIVERED.value,
                    http_status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    retry_count=retry_count,
                )
                logger.info(
                    "webhook_delivered",
                    alert_id=alert_id,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                )
            else:
                # HTTP error: retry only if the receiver might succeed later
                await self._handle_delivery_failure(
                    webhook_data,
                    f"HTTP {response.status_code}",
                    response.status_code,
                    response_time_ms,
                    retryable=is_retryable_status(response.status_code),
                )

        except UnsafeWebhookURL as e:
            # SSRF guard: never retried, and the attempt never left the server
            logger.warning("webhook_blocked_unsafe_url", alert_id=alert_id, reason=str(e))
            await self._handle_delivery_failure(
                webhook_data, f"Blocked: {e}", None, 0.0, retryable=False
            )
        except (httpx.TransportError, OSError) as e:
            # Network-level: timeout, DNS, refused, reset - transient, retry
            response_time_ms = (time.perf_counter() - start_time) * 1000
            await self._handle_delivery_failure(
                webhook_data, f"{type(e).__name__}: {e}", None, response_time_ms, retryable=True
            )
        except Exception as e:
            # e.g. invalid URL - retrying won't help
            response_time_ms = (time.perf_counter() - start_time) * 1000
            await self._handle_delivery_failure(
                webhook_data, f"{type(e).__name__}: {e}", None, response_time_ms, retryable=False
            )

    async def _send_webhook(
        self, url: str, payload: str, signature: str
    ) -> httpx.Response:
        """Send webhook HTTP request.

        Args:
            url: Webhook URL
            payload: JSON payload
            signature: HMAC signature

        Returns:
            HTTP response

        Raises:
            UnsafeWebhookURL: if the host resolves to a non-public address
        """
        if self.client is None:
            raise RuntimeError("WebhookDelivery not initialized")

        response = await self.client.post(
            url,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
                "User-Agent": "CryptoAlertAPI/1.0",
            },
        )
        return response

    async def _handle_delivery_failure(
        self,
        webhook_data: dict[str, Any],
        error_message: str,
        http_status_code: int | None,
        response_time_ms: float,
        retryable: bool = True,
    ) -> None:
        """Handle webhook delivery failure with retry logic.

        Never sleeps: retries are handed to a timer so this worker is
        immediately free for the next delivery.

        Args:
            webhook_data: Webhook delivery data
            error_message: Error message
            http_status_code: HTTP status code if available
            response_time_ms: Response time in milliseconds
            retryable: False for errors that will fail identically (e.g. 404)
        """
        alert_id = webhook_data["alert_id"]
        retry_count = webhook_data["retry_count"]

        if retryable and retry_count < settings.webhook_max_retries:
            retry_count += 1
            webhook_data["retry_count"] = retry_count

            backoff_delay = self._backoff_delay(retry_count)
            self._schedule_retry(webhook_data, backoff_delay)

            logger.warning(
                "webhook_retry_scheduled",
                alert_id=alert_id,
                retry_count=retry_count,
                backoff_delay=round(backoff_delay, 3),
            )

            # Log retry attempt
            await self._log_delivery(
                alert_id,
                WebhookStatus.RETRYING.value,
                http_status_code=http_status_code,
                response_time_ms=response_time_ms,
                error_message=error_message,
                retry_count=retry_count,
            )
        else:
            reason = "Max retries exceeded" if retryable else "Non-retryable error"
            logger.error(
                "webhook_delivery_failed",
                alert_id=alert_id,
                error=error_message,
                reason=reason,
                retries=retry_count,
            )

            # Log final failure
            await self._log_delivery(
                alert_id,
                WebhookStatus.FAILED.value,
                http_status_code=http_status_code,
                response_time_ms=response_time_ms,
                error_message=f"{reason}: {error_message}",
                retry_count=retry_count,
            )

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Exponential backoff with jitter: half fixed, half random.

        attempt 1 -> 1-2s, 2 -> 2-4s, 3 -> 4-8s (with 2s base). The random half
        spreads retries out so a recovering endpoint isn't hit all at once.
        """
        delay = settings.webhook_retry_delay_seconds * (2 ** (attempt - 1))
        return delay / 2 + random.uniform(0, delay / 2)

    def _schedule_retry(self, webhook_data: dict[str, Any], delay: float) -> None:
        """Re-queue after `delay` on a timer, without holding a worker."""
        task = asyncio.create_task(self._requeue_after(webhook_data, delay))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _requeue_after(self, webhook_data: dict[str, Any], delay: float) -> None:
        """Sleep then put back on the queue. Runs outside the workers, so if the
        queue is full it waits for workers to drain it instead of deadlocking."""
        await asyncio.sleep(delay)
        await self.delivery_queue.put(webhook_data)

    async def _log_delivery(
        self,
        alert_id: int,
        status: str,
        http_status_code: int | None = None,
        response_time_ms: float | None = None,
        error_message: str | None = None,
        retry_count: int = 0,
    ) -> None:
        """Log webhook delivery attempt.

        Opens its own session per write so it never shares a session with
        concurrent tasks. A logging failure is reported but never kills the worker.

        Args:
            alert_id: Alert ID
            status: Delivery status
            http_status_code: HTTP status code
            response_time_ms: Response time in milliseconds
            error_message: Error message if failed
            retry_count: Number of retry attempts
        """
        log = WebhookLog(
            alert_id=alert_id,
            status=status,
            http_status_code=http_status_code,
            response_time_ms=response_time_ms,
            error_message=error_message,
            retry_count=retry_count,
        )
        try:
            async with AsyncSessionLocal() as session:
                session.add(log)
                await session.commit()
        except Exception as e:
            logger.error("webhook_log_write_failed", alert_id=alert_id, error=str(e))

    async def get_queue_size(self) -> int:
        """Get current webhook queue size.

        Returns:
            Number of pending webhooks
        """
        return self.delivery_queue.qsize()
