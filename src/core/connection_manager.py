"""Manage WebSocket connections to multiple exchanges."""
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any
import structlog
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import AsyncSessionLocal
from src.models import Alert, AlertStatus
from src.exchanges.base import ExchangeConnection
from src.exchanges.binance import BinanceConnection
from src.exchanges.coinbase import CoinbaseConnection
from src.core.matcher import AlertMatcher

logger = structlog.get_logger()
settings = get_settings()


class ConnectionManager:
    """Manage WebSocket connections and route price updates to matcher.

    One connection per exchange, shared by every subscribed symbol.
    Multiplexes multiple alerts onto single connections for efficiency.
    """

    def __init__(self, matcher: AlertMatcher) -> None:
        """Initialize connection manager.

        Args:
            matcher: Alert matching engine
        """
        self.matcher = matcher
        self.connections: dict[str, dict[str, Any]] = {
            "binance": {},
            "coinbase": {},
        }
        self.symbol_subscriptions: dict[str, set[str]] = defaultdict(set)
        # Strong refs to in-flight match tasks: asyncio only keeps weak refs,
        # so un-referenced tasks can be garbage-collected mid-execution.
        self._pending_tasks: set[asyncio.Task[Any]] = set()

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
        if exchange == "binance":
            connection = BinanceConnection(settings.binance_ws_url, symbols)
        elif exchange == "coinbase":
            connection = CoinbaseConnection(settings.coinbase_ws_url, symbols)
        else:
            raise ValueError(f"Unsupported exchange: {exchange}")

        connection.add_callback(self._on_price_update)
        return connection

    async def _connect_in_background(self, exchange: str, connection: ExchangeConnection) -> None:
        """Connect, logging rather than raising (nobody awaits this task)."""
        try:
            await connection.connect()
            logger.info("created_connection", exchange=exchange, symbols=connection.symbols)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("exchange_connect_failed", exchange=exchange, error=str(e))

    def _on_price_update(self, symbol: str, price: float, timestamp: datetime) -> None:
        """Callback for price updates from exchanges.

        Args:
            symbol: Trading pair symbol
            price: Current price
            timestamp: Update timestamp
        """
        task = asyncio.create_task(self._match_with_own_session(symbol, price, timestamp))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

        logger.debug("price_update", symbol=symbol, price=price, timestamp=timestamp)

    async def _match_with_own_session(
        self, symbol: str, price: float, timestamp: datetime
    ) -> None:
        """Run matching for one price update in its own DB session.

        AsyncSession is not safe for concurrent use, and price updates arrive
        concurrently, so each update gets a short-lived session from the pool.
        """
        try:
            async with AsyncSessionLocal() as session:
                await self.matcher.match_price_update(symbol, price, timestamp, session)
        except Exception as e:
            logger.error("price_match_failed", symbol=symbol, price=price, error=str(e))

    async def disconnect_all(self) -> None:
        """Disconnect from all exchanges."""
        for task in list(self._connect_tasks):
            task.cancel()
        await asyncio.gather(*self._connect_tasks, return_exceptions=True)

        for exchange, conns in self.connections.items():
            for conn in conns.values():
                await conn.disconnect()
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
