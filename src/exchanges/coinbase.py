"""Coinbase Pro WebSocket connection implementation."""
import json
from datetime import datetime
import structlog

from src.exchanges.base import ExchangeConnection

logger = structlog.get_logger()


class CoinbaseConnection(ExchangeConnection):
    """Coinbase Pro exchange WebSocket connection."""

    async def _subscribe(self) -> None:
        """Subscribe to Coinbase Pro ticker channel."""
        if not self.ws:
            return

        # Coinbase uses product IDs like BTC-USD
        product_ids = [self._format_symbol(s) for s in self.symbols]

        subscribe_msg = {
            "type": "subscribe",
            "product_ids": product_ids,
            "channels": ["ticker"],
        }

        await self.ws.send(json.dumps(subscribe_msg))
        logger.info("subscribed_to_coinbase", product_ids=product_ids)

    def _format_symbol(self, symbol: str) -> str:
        """Convert standard symbol format to Coinbase format.

        Args:
            symbol: Symbol in format like BTCUSDT

        Returns:
            Coinbase format like BTC-USD
        """
        # Simple conversion: BTCUSDT -> BTC-USDT
        # This is a simplified version; production would need more robust mapping
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}-USDT"
        elif symbol.endswith("USD"):
            base = symbol[:-3]
            return f"{base}-USD"
        return symbol

    async def _parse_message(self, message: str) -> tuple[str, float, datetime] | None:
        """Parse Coinbase message format.

        Coinbase ticker message format:
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "50000.00",
            "time": "2023-01-01T00:00:00.000000Z"
        }
        """
        try:
            data = json.loads(message)

            if data.get("type") == "ticker":
                product_id = data["product_id"]
                # Convert back to standard format: BTC-USD -> BTCUSD
                symbol = product_id.replace("-", "")
                price = float(data["price"])
                timestamp = datetime.fromisoformat(data["time"].replace("Z", "+00:00"))
                return symbol, price, timestamp

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug("coinbase_parse_error", message=message, error=str(e))

        return None


async def create_coinbase_connection(ws_url: str, symbols: list[str]) -> CoinbaseConnection:
    """Factory function to create and connect to Coinbase.

    Args:
        ws_url: Coinbase WebSocket URL
        symbols: List of symbols to subscribe to

    Returns:
        Connected CoinbaseConnection instance
    """
    connection = CoinbaseConnection(ws_url, symbols)
    await connection.connect()
    return connection
