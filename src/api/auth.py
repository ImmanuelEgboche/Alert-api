"""Authentication and API key management.

API keys are 256-bit random secrets, so they are stored as a SHA-256 hash and
looked up by exact hash match (unique index, O(1)). The plaintext key is shown
to the user once at registration and never stored.

Why not bcrypt? bcrypt is deliberately slow (~200ms) to resist brute-forcing
low-entropy *passwords*. A 256-bit random key cannot be brute-forced, so a fast
hash is equally safe - and it keeps auth off the event loop's critical path.
"""
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.config import get_settings
from src.database import get_db
from src.models import User

# Recognisable prefix: lets secret scanners and humans spot leaked keys.
API_KEY_PREFIX = "cak_"
# Non-secret leading chars stored for display/support ("cak_1a2b3c4d...").
DISPLAY_PREFIX_LEN = len(API_KEY_PREFIX) + 8


def generate_api_key() -> str:
    """Generate a secure random API key: 'cak_' + 64 hex chars (256 bits)."""
    return API_KEY_PREFIX + secrets.token_hex(32)


def hash_api_key(api_key: str) -> str:
    """SHA-256 hex digest of the key - what gets stored and indexed."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def display_prefix(api_key: str) -> str:
    """Non-secret identifying prefix of a key, safe to store and show."""
    return api_key[:DISPLAY_PREFIX_LEN]


async def get_current_user(
    x_api_key: Annotated[str, Header(alias="X-API-Key")],
    session: AsyncSession = Depends(get_db),
) -> User:
    """Get current user from API key.

    Raises:
        HTTPException: 401 if key missing/invalid, 403 if user disabled
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "APIKey"},
        )

    # Exact lookup on the unique hash index. No plaintext involved, and the
    # comparison happens on a hash an attacker can't steer, so timing leaks nothing.
    result = await session.execute(
        select(User).where(User.api_key_hash == hash_api_key(x_api_key))
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_metrics_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard operator-only endpoints with `Authorization: Bearer <METRICS_TOKEN>`.

    A user API key is deliberately NOT accepted: these endpoints expose
    platform-wide stats, which no individual customer should see.

    Raises:
        HTTPException: 503 if no token is configured (fail closed),
            401 if the header is missing or wrong
    """
    configured = get_settings().metrics_token
    expected = configured.get_secret_value() if configured else ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metrics endpoint disabled: METRICS_TOKEN not configured",
        )

    scheme, _, token = (authorization or "").partition(" ")
    # compare_digest: constant-time, so response timing doesn't reveal how
    # many leading characters of a guess were correct
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing metrics token",
            headers={"WWW-Authenticate": "Bearer"},
        )
