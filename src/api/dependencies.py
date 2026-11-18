"""FastAPI dependency injection functions."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.matcher import AlertMatcher
    from src.core.connection_manager import ConnectionManager
    from src.exchanges.symbols import SymbolRegistry


async def get_symbol_registry() -> "SymbolRegistry":
    """Get the tradable-symbol registry. Overridden in main.py."""
    raise RuntimeError("SymbolRegistry dependency not configured")


async def get_alert_matcher() -> "AlertMatcher":
    """Get alert matcher instance.

    This function is overridden by app.dependency_overrides in main.py
    """
    raise RuntimeError("AlertMatcher dependency not configured")


async def get_connection_manager() -> "ConnectionManager":
    """Get connection manager instance.

    This function is overridden by app.dependency_overrides in main.py
    """
    raise RuntimeError("ConnectionManager dependency not configured")
