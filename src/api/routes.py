"""API route handlers."""
from datetime import datetime, timezone
from typing import Any, Literal

import asyncio
from collections.abc import Awaitable

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text
import structlog

from src.database import get_db, get_redis
from src.models import Alert, User, WebhookLog, AlertStatus, generate_webhook_secret
from src.schemas import (
    ApiKeyRotatedResponse,
    WebhookSecretRotatedResponse,
    AlertCreate,
    AlertResponse,
    AlertUpdate,
    UserCreate,
    UserCreatedResponse,
    UserResponse,
    WebhookLogResponse,
    HealthResponse,
    MetricsResponse,
)
from src.api.auth import (
    CurrentUser,
    display_prefix,
    generate_api_key,
    hash_api_key,
    require_metrics_token,
)
from src.config import get_settings
from src.api.dependencies import get_alert_matcher, get_connection_manager, get_symbol_registry
from src.core.matcher import AlertMatcher
from src.core.connection_manager import ConnectionManager
from src.exchanges.symbols import SymbolRegistry
from src.monitoring.metrics import uptime_seconds

logger = structlog.get_logger()
settings = get_settings()

router = APIRouter()

# Statuses visible through the API. Deleted alerts are soft-deleted rows; to
# the user they no longer exist.
VisibleStatus = Literal["active", "paused", "triggered", "expired"]


async def _get_owned_alert(session: AsyncSession, alert_id: int, user: User) -> Alert:
    """The user's alert, or 404 (also for deleted alerts and other users' alerts)."""
    result = await session.execute(
        select(Alert).where(
            Alert.id == alert_id,
            Alert.user_id == user.id,
            Alert.status != AlertStatus.DELETED.value,
        )
    )
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return alert


async def _check_alert_limit(matcher: AlertMatcher, user_id: int) -> None:
    """Enforce max active alerts per user (on create AND on reactivation)."""
    if await matcher.get_user_alert_count(user_id) >= settings.max_alerts_per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {settings.max_alerts_per_user} alerts per user",
        )


@router.post("/register", response_model=UserCreatedResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    user_data: UserCreate,
    session: AsyncSession = Depends(get_db),
) -> UserCreatedResponse:
    """Register a new user and generate API key.

    Args:
        user_data: User registration data
        session: Database session

    Returns:
        User with API key

    Raises:
        HTTPException: If email already registered
    """
    # Check if email exists
    result = await session.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Generate API key - only its hash and display prefix are persisted
    api_key = generate_api_key()

    webhook_secret = generate_webhook_secret()

    user = User(
        email=user_data.email,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=display_prefix(api_key),
        webhook_secret=webhook_secret,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    logger.info("user_registered", user_id=user.id, key_prefix=user.api_key_prefix)

    # The plaintext key exists only in this response
    return UserCreatedResponse(
        **UserResponse.model_validate(user).model_dump(),
        api_key=api_key,
        webhook_secret=webhook_secret,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: CurrentUser) -> User:
    """The authenticated user's account (never includes secrets)."""
    return current_user


@router.post("/me/api-key", response_model=ApiKeyRotatedResponse)
async def rotate_api_key(
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> ApiKeyRotatedResponse:
    """Replace the API key. The key used for this request stops working at once.

    Use it after a key leaks, or to recover access if you still hold the old
    key. A lost key with no copy can't be recovered: register again.
    """
    api_key = generate_api_key()
    current_user.api_key_hash = hash_api_key(api_key)
    current_user.api_key_prefix = display_prefix(api_key)
    session.add(current_user)
    await session.commit()

    logger.info("api_key_rotated", user_id=current_user.id, key_prefix=current_user.api_key_prefix)
    return ApiKeyRotatedResponse(api_key=api_key, api_key_prefix=current_user.api_key_prefix)


@router.post("/me/webhook-secret", response_model=WebhookSecretRotatedResponse)
async def rotate_webhook_secret(
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> WebhookSecretRotatedResponse:
    """Replace the webhook signing secret.

    Every delivery attempt from now on (including retries of earlier alerts) is
    signed with the new secret, so update your receiver promptly.
    """
    webhook_secret = generate_webhook_secret()
    current_user.webhook_secret = webhook_secret
    session.add(current_user)
    await session.commit()

    logger.info("webhook_secret_rotated", user_id=current_user.id)
    return WebhookSecretRotatedResponse(webhook_secret=webhook_secret)


@router.post("/alerts", response_model=AlertResponse, status_code=status.HTTP_201_CREATED)
async def create_alert(
    alert_data: AlertCreate,
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    matcher: AlertMatcher = Depends(get_alert_matcher),
    connection_manager: ConnectionManager = Depends(get_connection_manager),
    symbols: SymbolRegistry = Depends(get_symbol_registry),
) -> Alert:
    """Create a new price alert.

    Args:
        alert_data: Alert creation data
        current_user: Authenticated user
        session: Database session
        matcher: Alert matching engine
        connection_manager: WebSocket connection manager

    Returns:
        Created alert

    Raises:
        HTTPException: If user has reached max alerts limit
    """
    # An alert on a symbol the exchange doesn't trade could never fire
    tradable = symbols.is_tradable(alert_data.symbol)
    if tradable is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Symbol list not loaded yet; try again shortly",
            headers={"Retry-After": "30"},
        )
    if not tradable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{alert_data.symbol} is not a trading symbol on Binance",
        )

    await _check_alert_limit(matcher, current_user.id)

    # Create alert
    alert = Alert(
        user_id=current_user.id,
        symbol=alert_data.symbol,
        condition=alert_data.condition,
        threshold=alert_data.threshold,
        webhook_url=str(alert_data.webhook_url),
        is_recurring=alert_data.is_recurring,
        expires_at=alert_data.expires_at,
        status=AlertStatus.ACTIVE.value,
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    # Add to matcher
    await matcher.add_alert(alert)

    # Subscribe to symbol on exchange
    await connection_manager.subscribe_symbol(alert_data.symbol)

    logger.info(
        "alert_created",
        alert_id=alert.id,
        user_id=current_user.id,
        symbol=alert_data.symbol,
        condition=alert_data.condition,
    )

    return alert


@router.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    status_filter: VisibleStatus | None = Query(default=None, alias="status"),
    symbol: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Alert]:
    """List user's alerts with optional filters.

    Args:
        current_user: Authenticated user
        session: Database session
        status_filter: Filter by status
        symbol: Filter by symbol
        limit: Maximum number of results
        offset: Pagination offset

    Returns:
        List of alerts
    """
    query = select(Alert).where(
        Alert.user_id == current_user.id,
        Alert.status != AlertStatus.DELETED.value,
    )

    if status_filter:
        query = query.where(Alert.status == status_filter)
    if symbol:
        query = query.where(Alert.symbol == symbol.upper())

    query = query.order_by(Alert.created_at.desc()).limit(limit).offset(offset)

    result = await session.execute(query)
    alerts = result.scalars().all()

    return list(alerts)


@router.get("/alerts/{alert_id}", response_model=AlertResponse)
async def get_alert(
    alert_id: int,
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> Alert:
    """Get alert details by ID.

    Args:
        alert_id: Alert ID
        current_user: Authenticated user
        session: Database session

    Returns:
        Alert details

    Raises:
        HTTPException: If alert not found or unauthorized
    """
    alert = await _get_owned_alert(session, alert_id, current_user)

    return alert


@router.patch("/alerts/{alert_id}", response_model=AlertResponse)
async def update_alert(
    alert_id: int,
    alert_update: AlertUpdate,
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    matcher: AlertMatcher = Depends(get_alert_matcher),
    connection_manager: ConnectionManager = Depends(get_connection_manager),
) -> Alert:
    """Update an existing alert.

    Args:
        alert_id: Alert ID
        alert_update: Alert update data
        current_user: Authenticated user
        session: Database session
        matcher: Alert matching engine

    Returns:
        Updated alert

    Raises:
        HTTPException: If alert not found or unauthorized
    """
    alert = await _get_owned_alert(session, alert_id, current_user)

    update_data = {
        k: v for k, v in alert_update.model_dump(exclude_unset=True).items() if v is not None
    }
    # HttpUrl is an object, not a str; the column is a string
    if "webhook_url" in update_data:
        update_data["webhook_url"] = str(update_data["webhook_url"])

    reactivating = (
        update_data.get("status") == AlertStatus.ACTIVE.value
        and alert.status != AlertStatus.ACTIVE.value
    )
    if reactivating:
        expires_at = update_data.get("expires_at", alert.expires_at)
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Alert has expired; set a future expires_at to reactivate it",
            )
        await _check_alert_limit(matcher, current_user.id)

    for field, value in update_data.items():
        setattr(alert, field, value)

    await session.commit()
    await session.refresh(alert)

    # Update in matcher
    await matcher.remove_alert(alert.id, alert.symbol, alert.condition)
    if alert.status == AlertStatus.ACTIVE.value:
        await matcher.add_alert(alert)
        # A resumed alert may be the first active one for its symbol since
        # startup (e.g. paused across a restart) - make sure prices flow.
        await connection_manager.subscribe_symbol(alert.symbol)

    logger.info("alert_updated", alert_id=alert_id, user_id=current_user.id)
    return alert


@router.delete("/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(
    alert_id: int,
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    matcher: AlertMatcher = Depends(get_alert_matcher),
) -> None:
    """Delete an alert.

    Args:
        alert_id: Alert ID
        current_user: Authenticated user
        session: Database session
        matcher: Alert matching engine

    Raises:
        HTTPException: If alert not found or unauthorized
    """
    alert = await _get_owned_alert(session, alert_id, current_user)

    # Remove from matcher
    await matcher.remove_alert(alert.id, alert.symbol, alert.condition)

    # Soft delete
    alert.status = AlertStatus.DELETED.value
    await session.commit()

    logger.info("alert_deleted", alert_id=alert_id, user_id=current_user.id)


@router.get("/alerts/{alert_id}/logs", response_model=list[WebhookLogResponse])
async def get_alert_logs(
    alert_id: int,
    current_user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, le=200),
) -> list[WebhookLog]:
    """Get webhook delivery logs for an alert.

    Args:
        alert_id: Alert ID
        current_user: Authenticated user
        session: Database session
        limit: Maximum number of logs

    Returns:
        List of webhook logs

    Raises:
        HTTPException: If alert not found or unauthorized
    """
    await _get_owned_alert(session, alert_id, current_user)  # ownership check

    # Get logs
    result = await session.execute(
        select(WebhookLog)
        .where(WebhookLog.alert_id == alert_id)
        .order_by(WebhookLog.timestamp.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    return list(logs)


HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
# Service states that don't make the app unhealthy ("idle" = exchange unused)
OK_STATES = {"healthy", "idle"}


async def _probe(check: Awaitable[Any]) -> str:
    """healthy/unhealthy, with a timeout so a hung dependency can't hang /health."""
    try:
        await asyncio.wait_for(check, timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
        return "healthy"
    except Exception:
        return "unhealthy"


async def _ping_redis() -> None:
    redis = await get_redis()
    await redis.ping()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A required service is down"}},
)
async def health_check(
    response: Response,
    session: AsyncSession = Depends(get_db),
    connection_manager: ConnectionManager = Depends(get_connection_manager),
) -> dict[str, Any]:
    """Readiness: can this instance match prices and fire alerts right now?

    Returns 503 when any required service is down. Uptime monitors and load
    balancers act on the status code, not the body. Exchanges with no
    subscriptions report "idle" and don't count.
    """
    services = {
        "database": await _probe(session.execute(text("SELECT 1"))),
        "redis": await _probe(_ping_redis()),
        **await connection_manager.health_check(),
    }
    healthy = all(state in OK_STATES for state in services.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "healthy" if healthy else "unhealthy",
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc),
        "services": services,
    }


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    # Operator-only: platform-wide stats. User API keys are NOT accepted.
    dependencies=[Depends(require_metrics_token)],
)
async def get_metrics(
    session: AsyncSession = Depends(get_db),
    matcher: AlertMatcher = Depends(get_alert_matcher),
    connection_manager: ConnectionManager = Depends(get_connection_manager),
) -> dict[str, Any]:
    """Get platform-wide system metrics (operator token required).

    Args:
        session: Database session
        matcher: Alert matching engine
        connection_manager: WebSocket connection manager

    Returns:
        System metrics
    """
    # Active alerts
    active_alerts = await matcher.get_total_alerts()

    # Active connections
    connections = await connection_manager.get_active_connections()
    active_connections = sum(connections.values())

    # Today's stats
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(func.count())
        .select_from(Alert)
        .where(
            Alert.triggered_at >= today_start,
            Alert.status == AlertStatus.TRIGGERED.value,
        )
    )
    alerts_triggered_today = result.scalar() or 0

    result = await session.execute(
        select(func.count())
        .select_from(WebhookLog)
        .where(WebhookLog.timestamp >= today_start, WebhookLog.status == "delivered")
    )
    webhooks_delivered_today = result.scalar() or 0

    result = await session.execute(
        select(func.avg(WebhookLog.response_time_ms))
        .where(WebhookLog.timestamp >= today_start, WebhookLog.status == "delivered")
    )
    avg_latency_ms = result.scalar() or 0.0

    return {
        "active_alerts": active_alerts,
        "active_connections": active_connections,
        "alerts_triggered_today": alerts_triggered_today,
        "webhooks_delivered_today": webhooks_delivered_today,
        "avg_latency_ms": float(avg_latency_ms),
        "uptime_seconds": uptime_seconds(),
    }
