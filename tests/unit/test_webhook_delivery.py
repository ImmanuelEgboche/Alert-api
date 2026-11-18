"""Unit tests for webhook delivery: worker pool, non-blocking retries, retry policy."""
import asyncio
import time
from collections import Counter
from datetime import datetime

import httpx
import pytest

from src.models import WebhookStatus
from src.webhooks import delivery as dl_mod
from src.webhooks.delivery import WebhookDelivery

RETRY_DELAY = 0.2  # seconds; backoff for attempt n is 0.5-1x of RETRY_DELAY * 2^(n-1)


def alert(alert_id: int, url: str) -> dict:
    return {
        "id": str(alert_id), "symbol": "BTCUSDT", "condition": "price_above",
        "threshold": "50000", "webhook_url": url,
    }


@pytest.fixture
async def rig(monkeypatch, request):
    """WebhookDelivery wired to a fake HTTP endpoint; logs captured in memory.

    Endpoint behaviour is routed by URL path:
      /ok         -> 200
      /down       -> 500 always
      /flaky/N    -> 500 for the first N calls, then 200
      /missing    -> 404
      /slow       -> 200 after 0.3s
      /reset      -> connection error
    """
    monkeypatch.setattr(dl_mod.settings, "webhook_retry_delay_seconds", RETRY_DELAY)
    monkeypatch.setattr(dl_mod.settings, "webhook_max_retries", 3)
    # Worker count can be overridden per test: parametrize("rig", [n], indirect=True)
    monkeypatch.setattr(dl_mod.settings, "webhook_workers", getattr(request, "param", 10))

    calls: Counter = Counter()
    delivered_at: dict[int, float] = {}
    logs: list[tuple[int, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] += 1
        if path == "/down":
            return httpx.Response(500)
        if path.startswith("/flaky/"):
            fail_n = int(path.rsplit("/", 1)[1])
            return httpx.Response(500 if calls[path] <= fail_n else 200)
        if path == "/missing":
            return httpx.Response(404)
        if path == "/slow":
            await asyncio.sleep(0.3)
            return httpx.Response(200)
        if path == "/reset":
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200)

    async def fake_log(self, alert_id, status, **kw):
        logs.append((alert_id, status))
        if status == WebhookStatus.DELIVERED.value:
            delivered_at[alert_id] = time.perf_counter()

    monkeypatch.setattr(WebhookDelivery, "_log_delivery", fake_log)

    wd = WebhookDelivery()
    await wd.initialize()
    await wd.client.aclose()
    wd.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def send(alert_id: int, path: str):
        await wd.queue_webhook(alert(alert_id, f"https://hook.test{path}"), 51000.0, datetime.now())

    async def wait_for(predicate, timeout=5.0):
        deadline = time.perf_counter() + timeout
        while not predicate():
            assert time.perf_counter() < deadline, "timed out waiting for condition"
            await asyncio.sleep(0.01)

    yield wd, send, wait_for, calls, logs, delivered_at
    await wd.shutdown()


def statuses(logs, alert_id):
    return [s for a, s in logs if a == alert_id]


@pytest.mark.parametrize("rig", [1], indirect=True)  # ONE worker: isolates the backoff fix
async def test_failing_endpoint_does_not_block_others(rig):
    """The core bug: backoff used to sleep inside the worker. Even with a single
    worker, a retrying webhook must not delay the next one."""
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(1, "/down")   # will retry with backoff
    await wait_for(lambda: calls["/down"] >= 1)
    start = time.perf_counter()
    await send(2, "/ok")
    await wait_for(lambda: 2 in delivered_at)
    # Must not wait for alert 1's backoff (>= 0.1s) - well under it
    assert delivered_at[2] - start < 0.05


async def test_slow_endpoints_delivered_concurrently(rig):
    """5 x 0.3s endpoints: sequential would take 1.5s; a pool takes ~0.3s."""
    wd, send, wait_for, calls, logs, delivered_at = rig
    start = time.perf_counter()
    for i in range(5):
        await send(10 + i, "/slow")
    await wait_for(lambda: len(delivered_at) == 5)
    assert time.perf_counter() - start < 0.6


async def test_transient_failure_retried_then_delivered(rig):
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(3, "/flaky/2")
    await wait_for(lambda: 3 in delivered_at)
    assert calls["/flaky/2"] == 3
    assert statuses(logs, 3) == ["retrying", "retrying", "delivered"]


async def test_network_error_is_retried(rig):
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(4, "/reset")
    await wait_for(lambda: "failed" in statuses(logs, 4))
    assert calls["/reset"] == 1 + 3  # initial + max_retries


async def test_non_retryable_4xx_fails_immediately(rig):
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(5, "/missing")
    await wait_for(lambda: statuses(logs, 5))
    await asyncio.sleep(RETRY_DELAY)  # give any (wrong) retry a chance to happen
    assert calls["/missing"] == 1
    assert statuses(logs, 5) == ["failed"]


async def test_gives_up_after_max_retries(rig):
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(6, "/down")
    await wait_for(lambda: "failed" in statuses(logs, 6))
    assert calls["/down"] == 1 + 3
    assert statuses(logs, 6) == ["retrying"] * 3 + ["failed"]


async def test_shutdown_cancels_pending_retries(rig):
    wd, send, wait_for, calls, logs, delivered_at = rig
    await send(7, "/down")
    await wait_for(lambda: len(wd._retry_tasks) == 1)
    await wd.shutdown()
    assert not wd._retry_tasks and not wd._worker_tasks


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_backoff_is_exponential_with_jitter(attempt, monkeypatch):
    monkeypatch.setattr(dl_mod.settings, "webhook_retry_delay_seconds", 2.0)
    full = 2.0 * 2 ** (attempt - 1)
    delays = {WebhookDelivery._backoff_delay(attempt) for _ in range(50)}
    assert all(full / 2 <= d <= full for d in delays)
    assert len(delays) > 1  # jittered, not constant


@pytest.mark.parametrize(
    "code,expected",
    [(500, True), (503, True), (429, True), (408, True), (404, False), (401, False), (410, False)],
)
def test_retryable_status_policy(code, expected):
    assert dl_mod.is_retryable_status(code) is expected
