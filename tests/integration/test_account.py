"""Account endpoints: per-user webhook secrets and key rotation."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User
from src.webhooks.delivery import WebhookDelivery


async def register(client: AsyncClient, email: str = "a@example.com") -> dict:
    r = await client.post("/api/v1/register", json={"email": email})
    assert r.status_code == 201
    return r.json()


async def test_register_returns_a_per_user_webhook_secret(client: AsyncClient):
    a = await register(client, "a@example.com")
    b = await register(client, "b@example.com")
    assert a["webhook_secret"].startswith("whsec_") and len(a["webhook_secret"]) == 70
    assert a["webhook_secret"] != b["webhook_secret"]


async def test_me_never_exposes_secrets(client: AsyncClient):
    user = await register(client)
    r = await client.get("/api/v1/me", headers={"X-API-Key": user["api_key"]})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "a@example.com"
    assert "api_key" not in body and "webhook_secret" not in body


async def test_rotate_api_key_revokes_the_old_one(client: AsyncClient):
    user = await register(client)
    old = user["api_key"]

    r = await client.post("/api/v1/me/api-key", headers={"X-API-Key": old})
    assert r.status_code == 200
    new = r.json()["api_key"]
    assert new != old and new.startswith("cak_")
    assert r.json()["api_key_prefix"] == new[:12]

    assert (await client.get("/api/v1/me", headers={"X-API-Key": old})).status_code == 401
    assert (await client.get("/api/v1/me", headers={"X-API-Key": new})).status_code == 200


async def test_rotate_webhook_secret(client: AsyncClient, db_session: AsyncSession):
    user = await register(client)
    r = await client.post("/api/v1/me/webhook-secret", headers={"X-API-Key": user["api_key"]})
    assert r.status_code == 200
    new = r.json()["webhook_secret"]
    assert new != user["webhook_secret"] and new.startswith("whsec_")

    stored = await db_session.scalar(select(User.webhook_secret).where(User.id == user["id"]))
    assert stored == new


async def test_rotation_requires_auth(client: AsyncClient):
    for path in ("/api/v1/me/api-key", "/api/v1/me/webhook-secret"):
        r = await client.post(path, headers={"X-API-Key": "cak_wrong"})
        assert r.status_code == 401


async def test_webhook_verifies_with_the_owners_secret(monkeypatch):
    """The point of the change: a user can verify signatures with the secret
    they were given, and another user's secret doesn't verify them."""
    secrets_by_user = {1: "whsec_alice", 2: "whsec_bob"}
    received: list[httpx.Request] = []

    async def fake_secret(self, user_id):
        return secrets_by_user.get(user_id)

    async def fake_log(self, *a, **kw):
        pass

    monkeypatch.setattr(WebhookDelivery, "_get_signing_secret", fake_secret)
    monkeypatch.setattr(WebhookDelivery, "_log_delivery", fake_log)
    wd = WebhookDelivery()
    wd.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: received.append(r) or httpx.Response(200))
    )
    await wd._deliver_webhook({
        "alert_id": 7, "retry_count": 0, "current_price": 51000.0,
        "timestamp": datetime.now(timezone.utc),
        "alert_data": {"id": "7", "user_id": "1", "symbol": "BTCUSDT", "condition": "price_above",
                       "threshold": "50000", "webhook_url": "https://hooks.example.com/"},
    })
    await wd.client.aclose()

    # Verify exactly as the README tells users to
    body = received[0].content
    data = json.loads(body)
    claimed = data.pop("signature")
    canonical = json.dumps(data, separators=(",", ":"))

    def sign(secret: str) -> str:
        return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    assert hmac.compare_digest(claimed, sign("whsec_alice"))
    assert received[0].headers["X-Webhook-Signature"] == claimed
    assert not hmac.compare_digest(claimed, sign("whsec_bob"))


async def test_disabled_users_webhooks_are_not_sent(monkeypatch):
    sent: list[httpx.Request] = []
    logs: list[tuple] = []

    async def no_secret(self, user_id):
        return None

    async def fake_log(self, alert_id, status, **kw):
        logs.append((status, kw.get("error_message")))

    monkeypatch.setattr(WebhookDelivery, "_get_signing_secret", no_secret)
    monkeypatch.setattr(WebhookDelivery, "_log_delivery", fake_log)
    wd = WebhookDelivery()
    wd.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r)))
    await wd._deliver_webhook({
        "alert_id": 8, "retry_count": 0, "current_price": 1.0, "timestamp": datetime.now(),
        "alert_data": {"id": "8", "user_id": "9", "symbol": "BTCUSDT", "condition": "price_above",
                       "threshold": "1", "webhook_url": "https://hooks.example.com/"},
    })
    await wd.client.aclose()
    assert sent == []
    assert logs == [("failed", "Non-retryable error: Account disabled or deleted")]
    assert not wd._retry_tasks
