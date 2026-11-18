"""Pydantic schemas for request/response validation."""
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, EmailStr, field_validator

from src.config import get_settings
from src.webhooks.url_safety import check_url_static


# Conditions the matcher actually implements. price_change_percent exists in
# AlertCondition but has no matching logic (and no defined reference price), so
# accepting it would create an alert that can never fire.
SUPPORTED_CONDITIONS = ("price_above", "price_below")

# Statuses a user may set. triggered/expired are set by the system, and
# deleted only via DELETE.
UserSettableStatus = Literal["active", "paused"]


def validate_future_expiry(v: Optional[datetime]) -> Optional[datetime]:
    """expires_at must be in the future; naive datetimes are treated as UTC.

    Naive datetimes are ambiguous and can't be compared with the DB's
    timezone-aware values, so normalise them rather than store them as-is.
    """
    if v is None:
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    if v <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    return v


def validate_webhook_url(v: Optional[HttpUrl]) -> Optional[HttpUrl]:
    """Reject webhook URLs aimed at internal hosts (SSRF), with a clear 422.

    Delivery re-checks the resolved IP on every attempt; this is the early,
    DNS-free check so users hear about a bad URL when they set it.
    """
    if v is not None and not get_settings().webhook_allow_private_urls:
        check_url_static(str(v))
    return v


class UserCreate(BaseModel):
    """Schema for user creation."""

    email: EmailStr = Field(..., description="User email address")


class UserResponse(BaseModel):
    """Schema for user response."""

    id: int
    email: str
    api_key_prefix: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreatedResponse(UserResponse):
    """Registration response - the ONLY time the full API key is ever returned."""

    api_key: str = Field(..., description="Store this now; it cannot be retrieved again")


class AlertCreate(BaseModel):
    """Schema for alert creation."""

    symbol: str = Field(
        ..., min_length=3, max_length=20, description="Trading pair symbol (e.g., BTCUSDT)"
    )
    condition: str = Field(..., description="Alert condition: price_above or price_below")
    threshold: float = Field(..., gt=0, description="Price threshold for alert")
    webhook_url: HttpUrl = Field(..., description="Webhook URL to notify")
    is_recurring: bool = Field(default=False, description="Whether alert recurs after triggering")
    expires_at: Optional[datetime] = Field(
        default=None, description="Optional expiration timestamp"
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Normalize symbol; exchange symbols are uppercase alphanumeric."""
        v = v.upper().strip()
        if not v.isascii() or not v.isalnum():
            raise ValueError("Symbol must be letters and digits only (e.g. BTCUSDT)")
        return v

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, v: str) -> str:
        """Validate condition type."""
        if v == "price_change_percent":
            raise ValueError("price_change_percent is not supported yet")
        if v not in SUPPORTED_CONDITIONS:
            raise ValueError(f"Condition must be one of: {', '.join(SUPPORTED_CONDITIONS)}")
        return v

    _check_webhook_url = field_validator("webhook_url")(validate_webhook_url)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Reject past expiry; treat naive datetimes as UTC."""
        return validate_future_expiry(v)


class AlertUpdate(BaseModel):
    """Schema for alert updates."""

    threshold: Optional[float] = Field(default=None, gt=0)
    webhook_url: Optional[HttpUrl] = None
    status: Optional[UserSettableStatus] = None
    is_recurring: Optional[bool] = None
    expires_at: Optional[datetime] = None

    _check_webhook_url = field_validator("webhook_url")(validate_webhook_url)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Reject past expiry; treat naive datetimes as UTC."""
        return validate_future_expiry(v)


class AlertResponse(BaseModel):
    """Schema for alert response."""

    id: int
    user_id: int
    symbol: str
    condition: str
    threshold: float
    webhook_url: str
    status: str
    is_recurring: bool
    expires_at: Optional[datetime]
    triggered_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookLogResponse(BaseModel):
    """Schema for webhook log response."""

    id: int
    alert_id: int
    status: str
    http_status_code: Optional[int]
    response_time_ms: Optional[float]
    error_message: Optional[str]
    retry_count: int
    timestamp: datetime

    model_config = {"from_attributes": True}


class PriceUpdate(BaseModel):
    """Schema for price updates from exchanges."""

    symbol: str
    price: float
    timestamp: datetime
    exchange: str


class WebhookPayload(BaseModel):
    """Schema for webhook payload sent to users."""

    alert_id: int
    symbol: str
    condition: str
    threshold: float
    current_price: float
    triggered_at: datetime
    signature: str = Field(..., description="HMAC-SHA256 signature for verification")


class HealthResponse(BaseModel):
    """Schema for health check response."""

    status: str
    version: str
    timestamp: datetime
    services: dict[str, str]


class MetricsResponse(BaseModel):
    """Schema for metrics response."""

    active_alerts: int
    active_connections: int
    alerts_triggered_today: int
    webhooks_delivered_today: int
    avg_latency_ms: float
    uptime_seconds: float


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    error: str
    detail: Optional[str] = None
    timestamp: datetime
