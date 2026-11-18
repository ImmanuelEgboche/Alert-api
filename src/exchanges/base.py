"""Base class for exchange WebSocket connections."""
import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Any
import structlog

import websockets
from websockets.exceptions import WebSocketException
from tenacity import (
    retry,
    wait_exponential,
    retry_if_exception_type,
)

logger = structlog.get_logger()


class ExchangeConnection(ABC):
    """Abstract base class for exchange WebSocket connections."""

    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        """Initialize exchange connection.

        Args:
            ws_url: WebSocket URL for the exchange
            symbols: List of symbols to subscribe to
        """
        self.ws_url = ws_url
        self.symbols = symbols
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.is_connected = False
        self._callbacks: list[Callable[[str, float, datetime], None]] = []
        self._reconnect_task: asyncio.Task[Any] | None = None
        # Strong ref to the read loop (asyncio only keeps weak refs to tasks).
        self._handler_task: asyncio.Task[Any] | None = None
        # Set by disconnect(): an intentional close must not reconnect.
        self._closing = False
        # When the current connection opened, and how many connections in a
        # row have dropped before becoming stable (drives reconnect backoff).
        self._opened_at = 0.0
        self._quick_drops = 0

    # A connection that lived this long counts as healthy, so its drop (e.g.
    # Binance's 24h close) reconnects immediately. Shorter-lived ones back off.
    STABLE_AFTER_SECONDS = 30.0
    MAX_RECONNECT_DELAY_SECONDS = 60.0

    @abstractmethod
    async def _subscribe(self) -> None:
        """Subscribe to symbols. Must be implemented by subclasses."""
        pass

    @abstractmethod
    async def _parse_message(self, message: str) -> tuple[str, float, datetime] | None:
        """Parse exchange-specific message format.

        Args:
            message: Raw WebSocket message

        Returns:
            Tuple of (symbol, price, timestamp) or None if not a price update
        """
        pass

    def add_callback(self, callback: Callable[[str, float, datetime], None]) -> None:
        """Add callback for price updates.

        Args:
            callback: Function to call with (symbol, price, timestamp)
        """
        self._callbacks.append(callback)

    # Retry forever with capped exponential backoff: a price feed that gives up
    # permanently is worse than one that keeps trying every 60s. Network-level
    # failures (DNS, refused, handshake timeout) raise OSError/TimeoutError, not
    # WebSocketException, so they must be retried too.
    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type((WebSocketException, OSError, asyncio.TimeoutError)),
    )
    async def connect(self) -> None:
        """Connect to exchange WebSocket with retry logic."""
        try:
            logger.info("connecting_to_exchange", url=self.ws_url, symbols=self.symbols)
            self.ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10,
            )
            if self._closing:  # disconnect() was called while we were connecting
                await self.ws.close()
                return
            self.is_connected = True
            self._opened_at = time.monotonic()
            await self._subscribe()
            logger.info("connected_to_exchange", url=self.ws_url)

            self._handler_task = asyncio.create_task(self._message_handler())

        except Exception as e:
            logger.error("connection_failed", url=self.ws_url, error=str(e))
            self.is_connected = False
            raise

    async def _message_handler(self) -> None:
        """Read messages until the connection ends, then reconnect.

        The connection can end three ways, and all of them must reconnect:
          - clean close (1000/1001, e.g. Binance's 24h limit): `async for`
            just stops, no exception
          - abnormal close / keepalive ping timeout: ConnectionClosedError
          - anything unexpected escaping the loop
        Only an intentional disconnect() skips the reconnect.
        """
        ws = self.ws
        if ws is None:
            return

        try:
            async for message in ws:
                try:
                    parsed = await self._parse_message(message)
                    if parsed:
                        symbol, price, timestamp = parsed
                        # Call all registered callbacks
                        for callback in self._callbacks:
                            try:
                                callback(symbol, price, timestamp)
                            except Exception as e:
                                logger.error(
                                    "callback_error",
                                    symbol=symbol,
                                    price=price,
                                    error=str(e),
                                )
                except Exception as e:
                    logger.error("message_parse_error", message=message, error=str(e))

            logger.warning(
                "websocket_closed", url=self.ws_url,
                code=ws.close_code, reason=ws.close_reason,
            )
        except WebSocketException as e:
            logger.warning("websocket_disconnected", url=self.ws_url, error=str(e))
        except Exception as e:
            logger.error("websocket_handler_failed", url=self.ws_url, error=str(e))

        self.is_connected = False
        if self._closing:
            return
        self._reconnect()

    def _reconnect_delay(self) -> float:
        """0 after a stable connection; exponential backoff after quick drops."""
        if time.monotonic() - self._opened_at >= self.STABLE_AFTER_SECONDS:
            self._quick_drops = 0
            return 0.0
        self._quick_drops += 1
        return min(self.MAX_RECONNECT_DELAY_SECONDS, 2.0 ** (self._quick_drops - 1))

    def _reconnect(self) -> None:
        """Start reconnecting in the background (at most one attempt at a time)."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        delay = self._reconnect_delay()
        logger.info("reconnecting_to_exchange", url=self.ws_url, delay_seconds=delay)
        self._reconnect_task = asyncio.create_task(self._reconnect_after(delay))

    async def _reconnect_after(self, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            await self.connect()  # retries network errors forever
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("reconnect_failed", url=self.ws_url, error=str(e))

    async def disconnect(self) -> None:
        """Disconnect from exchange, without reconnecting."""
        self._closing = True
        self.is_connected = False
        tasks = [t for t in (self._reconnect_task, self._handler_task) if t and not t.done()]
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        if self.ws:
            await self.ws.close()
            logger.info("disconnected_from_exchange", url=self.ws_url)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def add_symbols(self, symbols: list[str]) -> None:
        """Add new symbols to subscription.

        Args:
            symbols: List of symbols to add
        """
        new_symbols = [s for s in symbols if s not in self.symbols]
        if not new_symbols:
            return

        self.symbols.extend(new_symbols)
        if self.is_connected:
            # Resubscribe with updated symbols
            await self._subscribe()
            logger.info("added_symbols", symbols=new_symbols, exchange=self.__class__.__name__)
