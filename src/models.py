"""Database models using SQLAlchemy."""
import secrets
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    String,
    Integer,
    Float,
    DateTime,
    Boolean,
    Text,
    Index,
    ForeignKey,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


WEBHOOK_SECRET_PREFIX = "whsec_"


def generate_webhook_secret() -> str:
    """Per-user webhook signing secret: 'whsec_' + 64 hex chars (256 bits)."""
    return WEBHOOK_SECRET_PREFIX + secrets.token_hex(32)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class AlertStatus(str, Enum):
    """Alert status enumeration."""

    ACTIVE = "active"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    PAUSED = "paused"
    DELETED = "deleted"


class AlertCondition(str, Enum):
    """Alert condition types."""

    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    PRICE_CHANGE_PERCENT = "price_change_percent"


class WebhookStatus(str, Enum):
    """Webhook delivery status."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


class User(Base):
    """User model."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # SHA-256 hex of the API key; the plaintext key is never stored.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Non-secret leading chars ("cak_1a2b3c4d") so users/support can tell keys apart.
    api_key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    # Signs this user's webhooks (HMAC-SHA256). Stored retrievable because the
    # server must sign with it; it is only ever returned by register/rotate.
    webhook_secret: Mapped[str] = mapped_column(
        String(80), nullable=False, default=generate_webhook_secret
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    alerts: Mapped[list["Alert"]] = relationship(
        "Alert", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email})>"


class Alert(Base):
    """Alert model."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("idx_user_status", "user_id", "status"),
        Index("idx_symbol_status", "symbol", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    condition: Mapped[str] = mapped_column(String(50), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=AlertStatus.ACTIVE.value, nullable=False, index=True
    )
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="alerts")
    webhook_logs: Mapped[list["WebhookLog"]] = relationship(
        "WebhookLog", back_populates="alert", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Alert(id={self.id}, symbol={self.symbol}, condition={self.condition})>"


class WebhookLog(Base):
    """Webhook delivery log model."""

    __tablename__ = "webhook_logs"
    __table_args__ = (Index("idx_alert_timestamp", "alert_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    alert_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    http_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    alert: Mapped["Alert"] = relationship("Alert", back_populates="webhook_logs")

    def __repr__(self) -> str:
        return f"<WebhookLog(id={self.id}, alert_id={self.alert_id}, status={self.status})>"


class Metric(Base):
    """System metrics model."""

    __tablename__ = "metrics"
    __table_args__ = (Index("idx_metric_timestamp", "metric_name", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<Metric(name={self.metric_name}, value={self.value})>"
