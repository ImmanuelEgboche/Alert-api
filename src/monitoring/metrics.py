"""Prometheus metrics collection."""
import time
from typing import Callable, Awaitable

from fastapi import Request, Response
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    REGISTRY,
    start_http_server,
)
import structlog

from src.config import get_settings

logger = structlog.get_logger()
settings = get_settings()

# Request metrics
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
)

# Alert metrics
alerts_triggered_total = Counter(
    "alerts_triggered_total",
    "Total alerts triggered",
    ["symbol", "condition"],
)

alert_matching_duration_seconds = Histogram(
    "alert_matching_duration_seconds",
    "Alert matching latency",
)

# Webhook metrics
webhooks_delivered_total = Counter(
    "webhooks_delivered_total",
    "Total webhooks delivered",
    ["status"],
)

webhook_delivery_duration_seconds = Histogram(
    "webhook_delivery_duration_seconds",
    "Webhook delivery latency",
)

webhook_queue_size = Gauge(
    "webhook_queue_size",
    "Current webhook queue size",
)

# Exchange metrics
websocket_connections_active = Gauge(
    "websocket_connections_active",
    "Active WebSocket connections",
    ["exchange"],
)

websocket_reconnections_total = Counter(
    "websocket_reconnections_total",
    "Total WebSocket reconnections",
    ["exchange"],
)

price_updates_processed_total = Counter(
    "price_updates_processed_total",
    "Total price updates processed",
    ["symbol", "exchange"],
)

# System metrics
active_alerts_count = Gauge(
    "active_alerts_count",
    "Number of active alerts",
)

memory_usage_bytes = Gauge(
    "memory_usage_bytes",
    "Memory usage in bytes",
)


_STARTED_AT = time.monotonic()  # module import ~= process start
_metrics_server_started = False


def uptime_seconds() -> float:
    """Seconds since the app process started (monotonic: immune to clock changes)."""
    return time.monotonic() - _STARTED_AT


def setup_metrics() -> None:
    """Serve Prometheus metrics on METRICS_PORT (what prometheus.yml scrapes).

    This port has no app-level auth by design (standard for Prometheus exporters):
    it must only be reachable on the internal network. docker-compose publishes
    it on 127.0.0.1 only; Prometheus scrapes it over the compose network.
    """
    global _metrics_server_started
    if _metrics_server_started:  # idempotent: a second bind would raise
        return
    start_http_server(settings.metrics_port)
    _metrics_server_started = True
    logger.info("prometheus_metrics_serving", port=settings.metrics_port)


def _route_template(request: Request) -> str:
    """Label by route template (/alerts/{alert_id}), never the raw path.

    Raw paths create one time series per alert ID - unbounded label
    cardinality that grows memory in both the app and Prometheus.
    """
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware to collect request count and latency per route."""
    method = request.method
    start_time = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        http_requests_total.labels(
            method=method, endpoint=_route_template(request), status_code=500
        ).inc()
        raise

    # Route is resolved during call_next, so read the template afterwards
    endpoint = _route_template(request)
    duration = time.perf_counter() - start_time
    http_requests_total.labels(
        method=method, endpoint=endpoint, status_code=response.status_code
    ).inc()
    http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)
    return response


def get_metrics() -> bytes:
    """Get Prometheus metrics.

    Returns:
        Metrics in Prometheus text format
    """
    return generate_latest(REGISTRY)
