"""Base class for exchange WebSocket connections."""
import asyncio
import inspect
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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


@dataclass(slots=True)
class Tick:
    """One trade, plus the timestamps needed to measure where time goes.

    Wall-clock times (``*_ns`` from time.time_ns) compare against the exchange's
    own clock; ``recv_mono_ns`` (perf_counter_ns) times our internal stages,
    which must not jump if NTP adjusts the wall clock.
    """

    symbol: str
    price: float
    timestamp: datetime              # exchange trade time (UTC)
    trade_id: int | None = None      # per-symbol sequence number, if the exchange has one
    exchange_ms: int | None = None   # exchange trade time, epoch ms
    recv_ns: int = 0                 # wall clock when the message arrived (or was recorded)
    recv_mono_ns: int = 0            # monotonic clock when we started handling it


# May be sync or async. An async callback is awaited before the next message is
# read, which is how a busy pipeline slows the reader instead of dropping trades.
TickCallback = Callable[[Tick], Any]
# (recv_wall_ns, raw message) - called for every message, before parsing
RawHook = Callable[[int, str], None]


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
        self._callbacks: list[TickCallback] = []
        self._raw_hooks: list[RawHook] = []
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
    async def _parse_message(self, message: str) -> Tick | None:
        """Parse exchange-specific message format.

        Args:
            message: Raw WebSocket message

        Returns:
            A Tick (receive times are filled in by _dispatch), or None if the
            message is not a trade (subscription acks, heartbeats, ...)
        """
        pass

    def add_callback(self, callback: TickCallback) -> None:
        """Add callback for price updates; called with each parsed Tick."""
        self._callbacks.append(callback)

    def add_raw_hook(self, hook: RawHook) -> None:
        """Add a hook that sees every raw message before parsing (the recorder)."""
        self._raw_hooks.append(hook)

    async def _dispatch(self, message: str, recv_ns: int | None = None) -> Tick | None:
        """Parse one raw message and hand the trade to every callback.

        The live WebSocket loop and the replay feed both come through here, so
        a replayed recording exercises exactly the same parsing and routing.

        Args:
            message: Raw message as the exchange sent it
            recv_ns: When it arrived (wall clock). Live: now. Replay: the time
                recorded, so exchange->receive delay can still be reported.
        """
        recv_mono_ns = time.perf_counter_ns()
        if recv_ns is None:
            recv_ns = time.time_ns()
        for hook in self._raw_hooks:
            try:
                hook(recv_ns, message)
            except Exception as e:
                logger.error("raw_hook_error", error=str(e))
        try:
            tick = await self._parse_message(message)
        except Exception as e:
            logger.error("message_parse_error", message=message, error=str(e))
            return None
        if tick is None:
            return None
        tick.recv_ns = recv_ns
        tick.recv_mono_ns = recv_mono_ns
        for callback in self._callbacks:
            try:
                result = callback(tick)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.error("callback_error", symbol=tick.symbol, price=tick.price, error=str(e))
        return tick

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
                await self._dispatch(message if isinstance(message, str) else message.decode())

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
