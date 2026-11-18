"""Manage WebSocket connections to multiple exchanges."""
import asyncio
import time
from collections import defaultdict
from typing import Any
import structlog
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import AsyncSessionLocal
from src.models import Alert, AlertStatus
from src.exchanges.base import ExchangeConnection, Tick
from src.exchanges.binance import BinanceConnection
from src.exchanges.coinbase import CoinbaseConnection
from src.core.matcher import AlertMatcher
from src.feed.completeness import TradeSequenceChecker
from src.monitoring.latency import LatencyRecorder, pipeline_latency

logger = structlog.get_logger()
settings = get_settings()


class ConnectionManager:
    """Manage WebSocket connections and route price updates to matcher.

    One connection per exchange, shared by every subscribed symbol.

    Matching runs on a fixed pool of workers, each with a bounded queue, and
    every symbol is always routed to the same worker:
      - a symbol's trades are matched in the order they happened
      - concurrency (and so Redis connections in use) is capped at the pool size
      - when the queues are full the feed reader waits (backpressure) rather
        than dropping trades. Live, that slows reads from the socket; replay
        at max speed runs exactly as fast as matching can keep up.
    (It used to start one task per trade: a burst of more than
    REDIS_MAX_CONNECTIONS trades failed with "Too many connections" and those
    prices were never matched. Found by replaying a recording at max speed.)
    """

    def __init__(
        self,
        matcher: AlertMatcher,
        latency: LatencyRecorder | None = None,
        sequence: TradeSequenceChecker | None = None,
    ) -> None:
        """Initialize connection manager.

        Args:
            matcher: Alert matching engine
            latency: Where stage timings go (default: the process-wide recorder)
            sequence: Trade-ID gap/duplicate tracker (default: a fresh one)
        """
        self.matcher = matcher
        self.latency = latency if latency is not None else pipeline_latency
        self.sequence = sequence if sequence is not None else TradeSequenceChecker()
        self.connections: dict[str, dict[str, Any]] = {
            "binance": {},
            "coinbase": {},
        }
        self.symbol_subscriptions: dict[str, set[str]] = defaultdict(set)

        # Match worker pool (started lazily: needs a running event loop)
        self._match_queues: list[asyncio.Queue[Tick]] = []
        self._match_workers: list[asyncio.Task[Any]] = []
        self._matching_now = 0

        # Background connect tasks (connect() retries forever, so it must never
        # be awaited inline by startup or a request handler).
        self._connect_tasks: set[asyncio.Task[Any]] = set()

    async def restore_subscriptions(self, session: AsyncSession) -> list[str]:
        """Re-subscribe to every symbol that has an active alert.

        Called on startup: alerts are reloaded into Redis, but without this no
        prices arrive to match them against.
        """
        result = await session.execute(
            select(distinct(Alert.symbol))
            .where(Alert.status == AlertStatus.ACTIVE.value)
            .order_by(Alert.symbol)
        )
        symbols = list(result.scalars().all())
        if symbols:
            await self.subscribe_symbols(symbols)
        logger.info("restored_subscriptions", count=len(symbols), symbols=symbols)
        return symbols

    async def subscribe_symbol(self, symbol: str, exchange: str = "binance") -> None:
        """Subscribe to a single symbol on an exchange."""
        await self.subscribe_symbols([symbol], exchange)

    async def subscribe_symbols(self, symbols: list[str], exchange: str = "binance") -> None:
        """Subscribe to symbols on an exchange, in one batch.

        Returns without waiting for the exchange to connect: the connection is
        opened in the background, and symbols added meanwhile are included in
        its SUBSCRIBE once it connects.

        Check-then-create below contains no `await`, so concurrent callers
        can't both see "no connection" and open duplicates (which would
        deliver every trade twice).
        """
        new = [s for s in dict.fromkeys(symbols) if s not in self.symbol_subscriptions[exchange]]
        if not new:
            return

        connection = self.connections[exchange].get(exchange)
        if connection is None:
            connection = self._build_connection(exchange, list(new))
            self.connections[exchange][exchange] = connection
            self.symbol_subscriptions[exchange].update(new)
            task = asyncio.create_task(self._connect_in_background(exchange, connection))
            self._connect_tasks.add(task)
            task.add_done_callback(self._connect_tasks.discard)
        else:
            self.symbol_subscriptions[exchange].update(new)
            await connection.add_symbols(new)

        logger.info("subscribed_to_symbols", symbols=new, exchange=exchange)

    async def unsubscribe_symbol(self, symbol: str, exchange: str = "binance") -> None:
        """Unsubscribe from a symbol.

        Args:
            symbol: Trading pair symbol
            exchange: Exchange name
        """
        self.symbol_subscriptions[exchange].discard(symbol)
        logger.info("unsubscribed_from_symbol", symbol=symbol, exchange=exchange)

    def _build_connection(self, exchange: str, symbols: list[str]) -> ExchangeConnection:
        """Construct (but don't connect) a connection to an exchange."""
        connection: ExchangeConnection
        if settings.feed_source == "replay" and exchange == "binance":
            # Same pipeline, recorded prices: plays only subscribed symbols,
            # and add_symbols() widens that just like a live SUBSCRIBE.
            from src.feed.replay import ReplayConnection
            connection = ReplayConnection(
                settings.replay_file or "", symbols, speed=settings.replay_speed
            )
        elif exchange == "binance":
            connection = BinanceConnection(settings.binance_ws_url, symbols)
        elif exchange == "coinbase":
            connection = CoinbaseConnection(settings.coinbase_ws_url, symbols)
        else:
            raise ValueError(f"Unsupported exchange: {exchange}")

        connection.add_callback(lambda tick: self._on_price_update(tick, exchange))
        return connection

    def attach_feed(self, connection: ExchangeConnection, exchange: str = "binance") -> None:
        """Route an externally built feed (e.g. a replay) into this pipeline."""
        connection.add_callback(lambda tick: self._on_price_update(tick, exchange))

    @property
    def pending_matches(self) -> int:
        """Trades queued or being matched right now."""
        return sum(q.qsize() for q in self._match_queues) + self._matching_now

    def _start_match_workers(self) -> None:
        n = settings.match_workers
        self._match_queues = [asyncio.Queue(maxsize=settings.match_queue_size) for _ in range(n)]
        self._match_workers = [
            asyncio.create_task(self._match_worker(q)) for q in self._match_queues
        ]

    async def _match_worker(self, queue: "asyncio.Queue[Tick]") -> None:
        while True:
            tick = await queue.get()
            self._matching_now += 1
            try:
                await self._match_with_own_session(tick)
            finally:
                self._matching_now -= 1
                queue.task_done()

    async def _connect_in_background(self, exchange: str, connection: ExchangeConnection) -> None:
        """Connect, logging rather than raising (nobody awaits this task)."""
        try:
            await connection.connect()
            logger.info("created_connection", exchange=exchange, symbols=connection.symbols)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("exchange_connect_failed", exchange=exchange, error=str(e))

    async def _on_price_update(self, tick: Tick, exchange: str = "binance") -> None:
        """Callback for each trade from an exchange (live or replayed).

        Awaited by the feed: returns once the trade is queued for matching,
        which waits if that symbol's worker is full (backpressure).
        """
        if tick.exchange_ms is not None and tick.recv_ns:
            self.latency.record("exchange_to_receive", tick.recv_ns - tick.exchange_ms * 1_000_000)

        if tick.trade_id is not None:
            kind, n = self.sequence.observe(f"{exchange}:{tick.symbol}", tick.trade_id)
            if kind == "gap":
                # Expected right after a reconnect; anywhere else, trades were lost
                logger.warning("trade_gap", exchange=exchange, symbol=tick.symbol,
                               missing=n, resumed_at_id=tick.trade_id)
            elif kind != "ok":
                logger.warning("trade_sequence_anomaly", exchange=exchange,
                               symbol=tick.symbol, kind=kind, trade_id=tick.trade_id)

        if not self._match_workers:
            self._start_match_workers()
        # Same symbol -> same worker -> matched in order
        queue = self._match_queues[hash(tick.symbol) % len(self._match_queues)]
        await queue.put(tick)

    async def _match_with_own_session(self, tick: Tick) -> None:
        """Run matching for one price update in its own DB session.

        AsyncSession is not safe for concurrent use, and price updates arrive
        concurrently, so each update gets a short-lived session from the pool
        (it only takes a DB connection if an alert actually fires).
        """
        start = time.perf_counter_ns()
        if tick.recv_mono_ns:
            self.latency.record("receive_to_match_start", start - tick.recv_mono_ns)
        try:
            async with AsyncSessionLocal() as session:
                await self.matcher.match_price_update(
                    tick.symbol, tick.price, tick.timestamp, session, tick=tick
                )
        except Exception as e:
            logger.error("price_match_failed", symbol=tick.symbol, price=tick.price, error=str(e))
        finally:
            self.latency.record("match", time.perf_counter_ns() - start)

    async def disconnect_all(self) -> None:
        """Disconnect from all exchanges."""
        for task in list(self._connect_tasks):
            task.cancel()
        await asyncio.gather(*self._connect_tasks, return_exceptions=True)

        for exchange, conns in self.connections.items():
            for conn in conns.values():
                await conn.disconnect()

        for task in self._match_workers:
            task.cancel()
        await asyncio.gather(*self._match_workers, return_exceptions=True)
        self._match_workers.clear()
        self._match_queues.clear()
        logger.info("disconnected_all_exchanges")

    async def get_active_connections(self) -> dict[str, int]:
        """Get count of active connections per exchange.

        Returns:
            Dictionary of exchange -> connection count
        """
        return {
            exchange: sum(1 for c in conns.values() if c.is_connected)
            for exchange, conns in self.connections.items()
        }

    async def health_check(self) -> dict[str, str]:
        """Status per exchange: healthy, disconnected, or idle.

        "idle" = no alerts use this exchange, so no connection is expected.
        It must not count as a failure, or an unused exchange (Coinbase today)
        makes the whole service look broken forever.
        """
        health = {}
        for exchange, conns in self.connections.items():
            if not conns:
                health[exchange] = "idle"
            else:
                all_connected = all(c.is_connected for c in conns.values())
                health[exchange] = "healthy" if all_connected else "disconnected"
        return health
