"""Application configuration using Pydantic settings."""
from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Values shipped in code or .env.example. Anyone can read them, so in
# production they're as good as no secret at all (webhook signatures forgeable).
PLACEHOLDER_SECRET_KEYS = {
    "change-me-in-production",
    "your-secret-key-change-in-production",
}
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "crypto-alert-api"
    app_version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _refuse_unsafe_production(self) -> "Settings":
        """Fail fast at startup rather than run production insecurely."""
        if self.environment != "production":
            return self
        problems = []
        if self.debug:
            problems.append("DEBUG must be false (it logs all SQL and returns error details)")
        if self.secret_key in PLACEHOLDER_SECRET_KEYS:
            problems.append("SECRET_KEY is a published placeholder")
        elif len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            problems.append(f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters")
        if self.webhook_allow_private_urls:
            problems.append(
                "WEBHOOK_ALLOW_PRIVATE_URLS must be false (users could reach internal services)"
            )
        if problems:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production: " + "; ".join(problems)
                + ". Generate a key with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return self

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_prefix: str = "/api/v1"

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/cryptoalert"
    )
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # Redis
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")
    redis_max_connections: int = 50

    # Security
    # Server-side secret. Webhooks are signed with each user's own
    # webhook_secret, not this; production still requires a strong value so
    # any future server-side signing never starts from a placeholder.
    secret_key: str = Field(default="change-me-in-production")
    api_key_length: int = 32

    # Rate Limiting
    rate_limit_per_minute: int = 100
    rate_limit_burst: int = 20

    # Webhooks
    webhook_timeout_seconds: int = 5
    webhook_max_retries: int = 3
    webhook_retry_delay_seconds: float = 2.0
    # Concurrent delivery workers; one slow endpoint only ties up one of them
    webhook_workers: int = Field(default=10, ge=1)
    # Allow webhooks to localhost/private networks. For local testing ONLY:
    # it lets any user make the server POST to internal services (SSRF).
    # Refused when ENVIRONMENT=production.
    webhook_allow_private_urls: bool = False

    # WebSocket Exchanges
    binance_ws_url: str = "wss://stream.binance.com:9443"
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    kraken_ws_url: str = "wss://ws.kraken.com"
    # REST API used to fetch the list of tradable symbols (alert validation)
    binance_rest_url: str = "https://api.binance.com"
    symbol_refresh_seconds: float = Field(default=3600, gt=0)

    # Monitoring
    metrics_enabled: bool = True
    metrics_port: int = 9090
    # Operator token for GET /api/v1/metrics (platform-wide stats). Unset = endpoint
    # disabled (fail closed). SecretStr keeps it out of logs/reprs.
    metrics_token: SecretStr | None = None

    # Performance
    max_alerts_per_user: int = 100
    max_webhook_queue_size: int = 10000
    # How often the expiry sweeper runs. Fire-time checks already stop expired alerts
    # firing; this bounds how long dead alerts count toward max_alerts_per_user.
    alert_cleanup_interval_seconds: float = Field(default=60, gt=0)
    # Recurring alerts re-arm only after price crosses back past threshold by this
    # fraction (0.001 = 0.1%). Stops a price wobbling around the threshold from
    # firing on every wobble. 0 = re-arm on any cross back.
    alert_rearm_margin: float = Field(default=0.001, ge=0, lt=1)

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == "production"

    @property
    def database_url_str(self) -> str:
        """Get database URL as string."""
        return str(self.database_url)

    @property
    def redis_url_str(self) -> str:
        """Get Redis URL as string."""
        return str(self.redis_url)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
