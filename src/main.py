"""Main FastAPI application."""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import structlog

from src.config import get_settings
from src.database import init_db, close_db, AsyncSessionLocal
from src.api.routes import router
from src.api import dependencies as deps
from src.core.matcher import AlertMatcher
from src.core.connection_manager import ConnectionManager
from src.core.expiry import expire_due_alerts, run_expiry_sweeper
from src.exchanges.symbols import SymbolRegistry
from src.webhooks.delivery import WebhookDelivery
from src.monitoring.metrics import setup_metrics, metrics_middleware

settings = get_settings()

# Configure structured logging. The filtering logger applies LOG_LEVEL;
# without it structlog emits everything, including a debug line per trade.
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
)

logger = structlog.get_logger()

# Global instances
alert_matcher: AlertMatcher | None = None
connection_manager: ConnectionManager | None = None
webhook_delivery: WebhookDelivery | None = None
symbol_registry = SymbolRegistry(settings.binance_rest_url, settings.symbol_refresh_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager.

    Handles startup and shutdown events.
    """
    global alert_matcher, connection_manager, webhook_delivery

    logger.info("starting_application", version=settings.app_version)

    # Initialize database
    await init_db()
    logger.info("database_initialized")

    # Initialize alert matcher
    alert_matcher = AlertMatcher()
    await alert_matcher.initialize()

    # Initialize webhook delivery
    webhook_delivery = WebhookDelivery()
    await webhook_delivery.initialize()

    # Register webhook callback with matcher
    alert_matcher.add_trigger_callback(webhook_delivery.queue_webhook)

    # Initialize connection manager
    connection_manager = ConnectionManager(alert_matcher)

    # Expire anything that lapsed while we were down, THEN load what's still
    # active - otherwise stale alerts get loaded back into Redis.
    # Then re-subscribe their symbols: Redis holding alerts is useless without
    # a price feed. This returns immediately; exchanges connect in the background.
    async with AsyncSessionLocal() as session:
        await expire_due_alerts(session, alert_matcher)
        await alert_matcher.load_active_alerts(session)
        await connection_manager.restore_subscriptions(session)

    # Tradable-symbol list for alert validation: loads in the background so
    # startup never waits on Binance's REST API.
    symbol_task = asyncio.create_task(symbol_registry.run())

    # Background expiry sweep (keeps Redis and per-user alert counts accurate)
    expiry_task = asyncio.create_task(
        run_expiry_sweeper(alert_matcher, settings.alert_cleanup_interval_seconds)
    )

    # Setup metrics
    if settings.metrics_enabled:
        setup_metrics()
        logger.info("metrics_enabled", port=settings.metrics_port)

    logger.info("application_started")

    yield

    # Shutdown
    logger.info("shutting_down_application")

    expiry_task.cancel()
    symbol_task.cancel()
    await asyncio.gather(expiry_task, symbol_task, return_exceptions=True)

    if connection_manager:
        await connection_manager.disconnect_all()

    if webhook_delivery:
        await webhook_delivery.shutdown()

    await close_db()

    logger.info("application_shutdown_complete")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Production-grade cryptocurrency alert API",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Metrics middleware
if settings.metrics_enabled:
    app.middleware("http")(metrics_middleware)


# Exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler."""
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=True,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "detail": str(exc) if settings.debug else "An unexpected error occurred",
        },
    )


# Dependency injection for global instances
async def get_alert_matcher() -> AlertMatcher:
    """Get alert matcher instance."""
    if alert_matcher is None:
        raise RuntimeError("AlertMatcher not initialized")
    return alert_matcher


async def get_connection_manager() -> ConnectionManager:
    """Get connection manager instance."""
    if connection_manager is None:
        raise RuntimeError("ConnectionManager not initialized")
    return connection_manager


async def get_webhook_delivery() -> WebhookDelivery:
    """Get webhook delivery instance."""
    if webhook_delivery is None:
        raise RuntimeError("WebhookDelivery not initialized")
    return webhook_delivery


# Override dependencies - override the functions in the dependencies module
app.dependency_overrides[deps.get_alert_matcher] = get_alert_matcher
app.dependency_overrides[deps.get_connection_manager] = get_connection_manager
app.dependency_overrides[deps.get_symbol_registry] = lambda: symbol_registry

# Include routers
app.include_router(router, prefix=settings.api_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": f"{settings.api_prefix}/docs" if settings.debug else "disabled",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
