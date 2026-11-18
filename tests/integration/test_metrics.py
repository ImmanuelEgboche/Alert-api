"""Tests for metrics: operator-token auth on /api/v1/metrics, Prometheus exporter, labels."""
import socket

import httpx
import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import display_prefix, generate_api_key, hash_api_key
from src.config import get_settings
from src.models import User
from src.monitoring import metrics as metrics_mod

TOKEN = "op-token-for-tests"
URL = "/api/v1/metrics"


@pytest.fixture
def token_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr(TOKEN))


async def test_disabled_when_no_token_configured(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", None)
    response = await client.get(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503


async def test_empty_token_counts_as_unconfigured(client: AsyncClient, monkeypatch):
    """METRICS_TOKEN= (empty, as in .env.example) must not mean 'empty bearer works'."""
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr(""))
    response = await client.get(URL, headers={"Authorization": "Bearer "})
    assert response.status_code == 503


@pytest.mark.parametrize(
    "headers",
    [
        {},                                             # no auth at all
        {"Authorization": "Bearer wrong-token"},         # wrong token
        {"Authorization": f"Basic {TOKEN}"},            # right secret, wrong scheme
        {"Authorization": TOKEN},                       # no scheme
    ],
)
async def test_rejects_bad_credentials(client: AsyncClient, token_configured, headers):
    response = await client.get(URL, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_user_api_key_is_not_enough(
    client: AsyncClient, db_session: AsyncSession, token_configured
):
    """Platform-wide stats must not be readable by any customer."""
    api_key = generate_api_key()
    db_session.add(User(email="u@example.com", api_key_hash=hash_api_key(api_key),
                        api_key_prefix=display_prefix(api_key)))
    await db_session.commit()
    response = await client.get(URL, headers={"X-API-Key": api_key})
    assert response.status_code == 401


async def test_valid_token_returns_metrics_with_uptime(client: AsyncClient, token_configured):
    response = await client.get(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    data = response.json()
    assert data["uptime_seconds"] > 0
    assert {"active_alerts", "alerts_triggered_today", "avg_latency_ms"} <= data.keys()


async def test_request_metrics_labelled_by_route_template(client: AsyncClient):
    """Per-ID paths must collapse into one series, not one per alert ID."""
    await client.get("/api/v1/alerts/987654")
    exposition = generate_latest(REGISTRY).decode()
    assert 'endpoint="/api/v1/alerts/{alert_id}"' in exposition
    assert "987654" not in exposition


def test_prometheus_exporter_serves_on_metrics_port(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(metrics_mod.settings, "metrics_port", port)
    monkeypatch.setattr(metrics_mod, "_metrics_server_started", False)

    metrics_mod.setup_metrics()
    metrics_mod.setup_metrics()  # idempotent: second call must not try to re-bind

    response = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
    assert response.status_code == 200
    assert "http_requests_total" in response.text
