"""Replay a recorded feed through the same pipeline as the live one.

ReplayConnection is an ExchangeConnection whose connect() reads a recording
instead of opening a WebSocket. Every message goes through the base class's
_dispatch() and the real exchange parser - the exact path live trades take -
so the matcher, Redis, DB and webhook delivery can't tell the difference.

Speed:
    1.0   real time: messages keep their recorded spacing
    N     N times faster (spacing divided by N)
    0     as fast as possible: no waiting, yielding to the event loop between
          messages so the pipeline's tasks still run

Each message keeps its *recorded* receive time, so exchange->receive delay is
reported from the original capture; every later stage is measured live.

Run directly to inspect a recording (parser only, no pipeline):

    python -m src.feed.replay recordings/binance-...jsonl.gz
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog

from src.exchanges.base import ExchangeConnection, Tick
from src.exchanges.binance import BinanceConnection
from src.exchanges.coinbase import CoinbaseConnection
from src.feed.completeness import TradeSequenceChecker
from src.feed.recording import read_header, read_messages
from src.monitoring.latency import LatencyRecorder

logger = structlog.get_logger()

PARSERS: dict[str, type[ExchangeConnection]] = {
    "binance": BinanceConnection,
    "coinbase": CoinbaseConnection,
}


@dataclass
class ReplayStats:
    messages: int = 0
    trades: int = 0           # messages that parsed into a trade on a replayed symbol
    recorded_seconds: float = 0.0   # span of the recording that was played
    wall_seconds: float = 0.0       # how long playing it took
    max_lag_ms: float = 0.0         # paced replay: worst lateness vs. the recorded schedule

    @property
    def speedup(self) -> float:
        return self.recorded_seconds / self.wall_seconds if self.wall_seconds else 0.0


class ReplayConnection(ExchangeConnection):
    """A feed that plays a recording file instead of a live socket."""

    # Max-speed replay yields to the event loop after every message, so tasks
    # created for one trade can start before the next trade is dispatched.
    def __init__(
        self,
        path: str | Path,
        symbols: list[str] | None = None,
        speed: float = 1.0,
    ) -> None:
        """
        Args:
            path: Recording file
            symbols: Only replay these (like a live SUBSCRIBE); None = everything
            speed: 1.0 real time, N = N x faster, 0 = as fast as possible
        """
        if speed < 0:
            raise ValueError("speed must be >= 0")
        self.path = Path(path)
        self.header = read_header(self.path)
        super().__init__(f"replay://{self.path}", list(symbols or []))
        self.replay_all = symbols is None
        self.speed = speed
        parser_cls = PARSERS.get(self.header["exchange"])
        if parser_cls is None:
            raise ValueError(f"no parser for exchange {self.header['exchange']!r}")
        # Unconnected instance, used only for its _parse_message
        self._parser = parser_cls(self.header.get("ws_url", ""), [])
        self.stats = ReplayStats()
        self.finished = asyncio.Event()

    async def connect(self) -> None:
        """Start playing in the background (returns immediately, like live connect)."""
        if self._handler_task is not None and not self._handler_task.done():
            return
        self.is_connected = True
        self._handler_task = asyncio.create_task(self._play())
        logger.info("replay_started", file=str(self.path), speed=self.speed or "max",
                    symbols="all" if self.replay_all else self.symbols)

    async def _subscribe(self) -> None:
        """Nothing to send: filtering happens in _parse_message."""

    async def _parse_message(self, message: str) -> Tick | None:
        tick = await self._parser._parse_message(message)
        if tick is None or (not self.replay_all and tick.symbol not in self.symbols):
            return None
        return tick

    async def _play(self) -> None:
        stats = self.stats
        first_recv_ns: int | None = None
        last_recv_ns = 0
        start = time.perf_counter()
        try:
            for rec in read_messages(self.path):
                if first_recv_ns is None:
                    first_recv_ns = rec.recv_ns
                last_recv_ns = rec.recv_ns
                if self.speed > 0:
                    due = start + (rec.recv_ns - first_recv_ns) / 1e9 / self.speed
                    delay = due - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    else:
                        stats.max_lag_ms = max(stats.max_lag_ms, -delay * 1000)
                stats.messages += 1
                if await self._dispatch(rec.raw, recv_ns=rec.recv_ns) is not None:
                    stats.trades += 1
                if self.speed == 0:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("replay_failed", file=str(self.path), error=str(e))
        finally:
            stats.wall_seconds = time.perf_counter() - start
            if first_recv_ns is not None:
                stats.recorded_seconds = (last_recv_ns - first_recv_ns) / 1e9
            self.is_connected = False
            self.finished.set()
            logger.info("replay_finished", **asdict(stats))

    async def disconnect(self) -> None:
        self._closing = True
        self.is_connected = False
        if self._handler_task is not None and not self._handler_task.done():
            self._handler_task.cancel()
            await asyncio.gather(self._handler_task, return_exceptions=True)


async def inspect(path: Path) -> dict[str, Any]:
    """Parse a whole recording: counts, completeness, recorded network delay."""
    conn = ReplayConnection(path, speed=0)
    checker = TradeSequenceChecker()
    latency = LatencyRecorder(max_samples=None)
    per_symbol: dict[str, int] = {}

    def on_tick(tick: Tick) -> None:
        per_symbol[tick.symbol] = per_symbol.get(tick.symbol, 0) + 1
        if tick.trade_id is not None:
            checker.observe(tick.symbol, tick.trade_id)
        if tick.exchange_ms is not None:
            latency.record("exchange_to_receive", tick.recv_ns - tick.exchange_ms * 1_000_000)

    conn.add_callback(on_tick)
    await conn.connect()
    await conn.finished.wait()
    return {
        "header": conn.header,
        "messages": conn.stats.messages,
        "trades": conn.stats.trades,
        "recorded_seconds": round(conn.stats.recorded_seconds, 1),
        "trades_per_symbol": dict(sorted(per_symbol.items())),
        "completeness": checker.report(),
        "latency": latency.summary(),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Summarise a recording (parser only).")
    p.add_argument("recording", type=Path)
    args = p.parse_args(argv)
    print(json.dumps(asyncio.run(inspect(args.recording)), indent=2, default=str))


if __name__ == "__main__":
    main()
