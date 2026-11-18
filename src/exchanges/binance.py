"""Binance WebSocket connection implementation."""
import json
from datetime import datetime, timezone
import structlog

from src.exchanges.base import ExchangeConnection, Tick

logger = structlog.get_logger()


class BinanceConnection(ExchangeConnection):
    """Binance exchange WebSocket connection."""

    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        # Binance only accepts connections on /ws (raw) or /stream (combined).
        # The bare host (wss://stream.binance.com:9443) rejects the handshake,
        # so default to /ws and add streams via the SUBSCRIBE message below.
        ws_url = ws_url.rstrip("/")
        if not ws_url.endswith(("/ws", "/stream")):
            ws_url = f"{ws_url}/ws"
        super().__init__(ws_url, symbols)

    async def _subscribe(self) -> None:
        """Subscribe to Binance trade streams via the live-subscription API."""
        if not self.ws:
            return

        # Binance uses stream names like: btcusdt@trade
        streams = [f"{symbol.lower()}@trade" for symbol in self.symbols]

        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1,
        }

        await self.ws.send(json.dumps(subscribe_msg))
        logger.info("subscribed_to_binance", streams=streams)

    async def _parse_message(self, message: str) -> Tick | None:
        """Parse Binance message format.

        Binance trade message format:
        {
            "e": "trade",
            "E": 1234567890001,   # event time (ms)
            "s": "BTCUSDT",
            "t": 12345,           # trade ID: +1 per trade, per symbol
            "p": "50000.00",
            "T": 1234567890000    # trade time (ms)
        }
        """
        try:
            data = json.loads(message)

            # Handle stream wrapper format
            if "data" in data:
                data = data["data"]

            if data.get("e") == "trade":
                trade_ms = int(data["T"])
                trade_id = data.get("t")
                return Tick(
                    symbol=data["s"],
                    price=float(data["p"]),
                    # UTC-aware: a naive local time would be stored wrong in
                    # timestamptz columns (triggered_at) on a non-UTC server
                    timestamp=datetime.fromtimestamp(trade_ms / 1000, tz=timezone.utc),
                    trade_id=int(trade_id) if trade_id is not None else None,
                    exchange_ms=trade_ms,
                )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug("binance_parse_error", message=message, error=str(e))

        return None


async def create_binance_connection(ws_url: str, symbols: list[str]) -> BinanceConnection:
    """Factory function to create and connect to Binance.

    Args:
        ws_url: Binance WebSocket URL
        symbols: List of symbols to subscribe to

    Returns:
        Connected BinanceConnection instance
    """
    connection = BinanceConnection(ws_url, symbols)
    await connection.connect()
    return connection
