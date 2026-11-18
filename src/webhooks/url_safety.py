"""SSRF protection for user-supplied webhook URLs.

Users choose where the server sends POST requests, so without checks they can
aim it at internal services: the cloud metadata endpoint (169.254.169.254),
Redis/Postgres on localhost, or anything on the private network.

Two layers:

1. ``check_url_static`` runs when an alert is created or updated. It rejects
   obviously internal targets (IP literals, ``localhost``, ``.internal``) with
   a clear 422, without doing any DNS.
2. ``SSRFSafeBackend`` runs at delivery time, on every new TCP connection.
   It resolves the hostname, refuses if ANY address is non-public, and
   connects to the vetted IP itself, so a DNS answer that changes between the
   check and the connect (DNS rebinding) can't redirect it.

Redirects are never followed (httpx default), so a public endpoint can't bounce
the request to an internal one.
"""
import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Hostnames that mean "this machine / this network" without needing DNS.
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")

# 64:ff9b::/96 - NAT64 well-known prefix; the last 32 bits are an IPv4 address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeWebhookURL(ValueError):
    """The webhook URL points somewhere the server must not send requests."""


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address hidden inside an IPv6 one, if any.

    ``::ffff:127.0.0.1``, ``2002:7f00:1::`` (6to4) and ``64:ff9b::7f00:1``
    (NAT64) all reach 127.0.0.1 on some networks, and ``is_global`` alone
    doesn't catch the last two.
    """
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None:
            return is_public_ip(inner)
    return ip.is_global and not ip.is_multicast


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def check_url_static(url: str) -> None:
    """Reject URLs that are unsafe without needing DNS.

    Raises:
        UnsafeWebhookURL: with a message safe to show the user
    """
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURL("Webhook URL must use http or https")
    if parsed.userinfo:
        raise UnsafeWebhookURL("Webhook URL must not contain credentials")

    host = parsed.host.lower().rstrip(".")
    if not host:
        raise UnsafeWebhookURL("Webhook URL has no host")

    ip = _parse_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise UnsafeWebhookURL("Webhook URL must not point to a private or reserved address")
        return

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeWebhookURL("Webhook URL must not point to a local or internal host")
    if "." not in host:
        # Single-label names ("redis", "postgres") resolve via the local
        # network's search domains - in Docker, to sibling containers.
        raise UnsafeWebhookURL("Webhook URL must use a fully qualified domain name")


async def vet_host(host: str, port: int) -> str:
    """Resolve ``host`` and return one vetted public IP to connect to.

    Every resolved address must be public. Rejecting on any private answer
    (rather than just skipping it) means a hostname can't mix a public and an
    internal address and hope the client picks the internal one.

    Raises:
        UnsafeWebhookURL: if the host is blocked or resolves to a non-public address
        OSError: if DNS resolution fails (transient; worth retrying)
    """
    literal = _parse_ip(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeWebhookURL(f"{host} is not a public address")
        return str(literal)

    name = host.lower().rstrip(".")
    if name in BLOCKED_HOSTNAMES or name.endswith(BLOCKED_SUFFIXES) or "." not in name:
        raise UnsafeWebhookURL(f"{host} is a local or internal host")

    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses:
        raise OSError(f"No addresses found for {host}")

    blocked = [a for a in addresses if not is_public_ip(a)]
    if blocked:
        raise UnsafeWebhookURL(f"{host} resolves to a non-public address ({blocked[0]})")

    # Prefer IPv4: IPv6 egress is often missing in containers
    addresses.sort(key=lambda a: a.version)
    return str(addresses[0])


class SSRFSafeBackend(httpcore.AsyncNetworkBackend):
    """Network backend that vets the address of every TCP connection it opens.

    The check and the connect use the same IP, so DNS rebinding (a public
    answer for the check, then a private one for the connect) can't work. The
    request URL keeps its real hostname, so TLS still verifies the certificate
    for that hostname, and connection pooling stays per-hostname.
    """

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        ip = await vet_host(host, port)
        return await self._inner.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeWebhookURL("Unix sockets are not allowed for webhooks")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def create_safe_transport(limits: httpx.Limits) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose every connection goes through SSRFSafeBackend.

    httpx 0.26 has no public option for a custom network backend, so the
    connection pool is replaced after construction (``_pool``; httpx is pinned
    in requirements.txt, and tests/unit/test_url_safety.py covers this path).
    Proxies from the environment are ignored (trust_env=False): a proxy would
    make the connect go to the proxy instead of the vetted address.
    """
    transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        network_backend=SSRFSafeBackend(),
    )
    return transport
