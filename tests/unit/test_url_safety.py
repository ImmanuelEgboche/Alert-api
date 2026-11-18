"""SSRF guard: which webhook targets are refused, at creation and at delivery."""
import ipaddress
import socket
from datetime import datetime

import httpcore
import httpx
import pytest

from src.models import WebhookStatus
from src.webhooks import url_safety
from src.webhooks.delivery import WebhookDelivery
from src.webhooks.url_safety import (
    SSRFSafeBackend,
    UnsafeWebhookURL,
    check_url_static,
    create_safe_transport,
    is_public_ip,
    vet_host,
)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1",
        "169.254.169.254",       # cloud metadata
        "100.64.0.1",            # carrier-grade NAT
        "0.0.0.0", "255.255.255.255", "224.0.0.1", "239.1.1.1",
        "::1", "fd00::1", "fe80::1", "ff02::1",
        "::ffff:169.254.169.254",  # IPv4-mapped
        "2002:7f00:1::",           # 6to4 wrapping 127.0.0.1
        "64:ff9b::a9fe:a9fe",      # NAT64 wrapping 169.254.169.254
    ],
)
def test_non_public_ips_are_refused(addr):
    assert not is_public_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_ips_are_allowed(addr):
    assert is_public_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6379/",
        "http://[::1]/",
        "http://localhost/hook",
        "http://LOCALHOST./hook",
        "http://foo.localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://redis:6379/",            # single label -> Docker sibling
        "https://user:pw@example.com/",  # credentials in URL
        "ftp://example.com/",
    ],
)
def test_static_check_refuses_internal_targets(url):
    with pytest.raises(UnsafeWebhookURL):
        check_url_static(url)


@pytest.mark.parametrize(
    "url", ["https://example.com/hook", "http://8.8.8.8/x", "https://hooks.example.org:8443/a"]
)
def test_static_check_allows_public_targets(url):
    check_url_static(url)


def fake_dns(monkeypatch, *addrs: str):
    """Make getaddrinfo return these addresses for any host."""

    async def getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs]

    monkeypatch.setattr(type(url_safety.asyncio.get_running_loop()), "getaddrinfo", getaddrinfo)


async def test_vet_host_returns_public_ip(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    assert await vet_host("example.com", 443) == "93.184.216.34"


async def test_hostname_resolving_to_metadata_ip_is_refused(monkeypatch):
    """The verified attack: a public-looking name that points at 169.254.169.254."""
    fake_dns(monkeypatch, "169.254.169.254")
    with pytest.raises(UnsafeWebhookURL, match="non-public"):
        await vet_host("evil.example.com", 443)


async def test_any_private_answer_refuses_whole_host(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34", "10.0.0.1")
    with pytest.raises(UnsafeWebhookURL):
        await vet_host("mixed.example.com", 443)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "redis"])
async def test_vet_host_refuses_without_dns(host):
    with pytest.raises(UnsafeWebhookURL):
        await vet_host(host, 80)


async def test_backend_connects_to_the_ip_it_vetted(monkeypatch):
    """DNS rebinding: the backend must connect to the address it checked, not
    hand the hostname on for a second (possibly different) lookup."""
    fake_dns(monkeypatch, "93.184.216.34")
    connected: list[str] = []

    async def fake_connect(self, host, port, **kw):
        connected.append(host)
        raise httpcore.ConnectError("stop here")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    with pytest.raises(httpcore.ConnectError):
        await SSRFSafeBackend().connect_tcp("hooks.example.com", 443)
    assert connected == ["93.184.216.34"]


async def test_blocked_delivery_is_not_sent_or_retried(monkeypatch):
    """End to end through the real transport: nothing leaves the server."""
    fake_dns(monkeypatch, "169.254.169.254")
    connects: list[str] = []
    monkeypatch.setattr(
        httpcore.AnyIOBackend, "connect_tcp", lambda self, host, *a, **k: connects.append(host)
    )
    logs: list[tuple[str, str | None]] = []

    async def fake_log(self, alert_id, status, **kw):
        logs.append((status, kw.get("error_message")))

    monkeypatch.setattr(WebhookDelivery, "_log_delivery", fake_log)
    wd = WebhookDelivery()
    wd.client = httpx.AsyncClient(transport=create_safe_transport(httpx.Limits()))
    await wd._deliver_webhook({
        "alert_id": 1, "retry_count": 0, "current_price": 1.0, "timestamp": datetime.now(),
        "alert_data": {"id": "1", "symbol": "BTCUSDT", "condition": "price_above",
                       "threshold": "1", "webhook_url": "https://evil.example.com/"},
    })
    assert connects == []
    assert logs[0][0] == WebhookStatus.FAILED.value
    assert "Blocked" in logs[0][1]
    assert not wd._retry_tasks
    await wd.client.aclose()


async def test_webhook_client_uses_safe_transport():
    """Guards the httpx private-attribute swap: if an httpx upgrade changes it,
    this fails instead of SSRF protection silently turning off."""
    wd = WebhookDelivery()
    await wd.initialize()
    try:
        pool = wd.client._transport._pool
        assert isinstance(pool._network_backend, SSRFSafeBackend)
        assert wd.client.follow_redirects is False
    finally:
        await wd.shutdown()
