"""Which symbols the exchange actually trades.

An alert on a symbol Binance doesn't trade (a typo, a delisted pair) is
accepted, stored and subscribed to, but can never fire. Validate against
Binance's live list instead.

The list is ~6 MB, so it's loaded once in the background and refreshed
periodically - never fetched inside a request, and never blocking startup.
"""
import asyncio
import json
from typing import Iterable

import httpx
import structlog

logger = structlog.get_logger()

# Retry a failed first load quickly; once loaded, a failed refresh just keeps
# the previous list until the next interval.
INITIAL_RETRY_SECONDS = 30.0


class SymbolRegistry:
    """In-memory set of currently trading symbols."""

    def __init__(
        self,
        rest_url: str = "https://api.binance.com",
        refresh_seconds: float = 3600.0,
        symbols: Iterable[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,  # tests inject a mock
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.refresh_seconds = refresh_seconds
        self._symbols: frozenset[str] | None = frozenset(symbols) if symbols is not None else None
        self._transport = transport

    @property
    def loaded(self) -> bool:
        return self._symbols is not None

    def is_tradable(self, symbol: str) -> bool | None:
        """True/False, or None if the list hasn't loaded yet (can't tell)."""
        if self._symbols is None:
            return None
        return symbol in self._symbols

    async def refresh(self) -> int:
        """Fetch the trading symbol list. Raises on any failure."""
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.get(
                f"{self.rest_url}/api/v3/exchangeInfo",
                params={"symbolStatus": "TRADING"},
            )
            response.raise_for_status()
        # Parsing ~6 MB of JSON takes long enough to stall the event loop.
        data = await asyncio.to_thread(json.loads, response.content)
        symbols = frozenset(
            s["symbol"] for s in data["symbols"] if s.get("status") == "TRADING"
        )
        if not symbols:
            # Never replace a good list with an empty one: that would reject
            # every alert.
            raise ValueError("exchange returned no trading symbols")
        self._symbols = symbols
        logger.info("symbols_refreshed", count=len(symbols))
        return len(symbols)

    async def run(self) -> None:
        """Load now, then refresh forever. Never raises (except cancellation)."""
        while True:
            try:
                await self.refresh()
                delay = self.refresh_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = self.refresh_seconds if self.loaded else INITIAL_RETRY_SECONDS
                logger.error("symbols_refresh_failed", error=str(e), retry_in=delay)
            await asyncio.sleep(delay)
