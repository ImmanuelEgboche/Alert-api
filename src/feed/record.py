"""Record a live exchange feed to disk.

    python -m src.feed.record --symbols BTCUSDT,ETHUSDT --duration 3600 \
        --out recordings/binance-1h.jsonl.gz

Uses the app's own BinanceConnection, so reconnects behave exactly as in
production (and show up as trade-ID gaps in the summary). Stops after
--duration seconds or on Ctrl-C; either way the file is closed cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.config import get_settings
from src.exchanges.base import Tick
from src.exchanges.binance import BinanceConnection
from src.feed.completeness import TradeSequenceChecker
from src.feed.recording import RecordingWriter

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT"


async def record(symbols: list[str], duration: float, out: Path, progress_every: float) -> dict:
    settings = get_settings()
    conn = BinanceConnection(settings.binance_ws_url, list(symbols))
    writer = RecordingWriter(out, "binance", symbols, conn.ws_url)
    checker = TradeSequenceChecker()
    trades = 0
    reconnects = -1  # the first connect isn't a reconnect

    def on_tick(tick: Tick) -> None:
        nonlocal trades
        trades += 1
        if tick.trade_id is not None:
            kind, n = checker.observe(tick.symbol, tick.trade_id)
            if kind == "gap":
                print(f"  gap: {tick.symbol} missing {n} trades before id {tick.trade_id}",
                      file=sys.stderr)

    conn.add_raw_hook(writer.write)
    conn.add_callback(on_tick)

    # Count reconnects by wrapping connect (it's called again after every drop)
    real_connect = conn.connect

    async def counting_connect() -> None:
        nonlocal reconnects
        reconnects += 1
        await real_connect()

    conn.connect = counting_connect  # type: ignore[method-assign]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await conn.connect()
    started = time.monotonic()  # after connect: --duration is time spent recording
    try:
        while not stop.is_set():
            elapsed = time.monotonic() - started
            if elapsed >= duration:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(progress_every, duration - elapsed))
            except asyncio.TimeoutError:
                pass
            print(f"[{time.monotonic() - started:7.0f}s] {writer.messages} messages, "
                  f"{trades} trades, {reconnects} reconnects", file=sys.stderr)
    finally:
        await conn.disconnect()
        writer.close()

    return {
        "file": str(out),
        "symbols": symbols,
        "duration_seconds": round(time.monotonic() - started, 1),
        "messages": writer.messages,
        "trades": trades,
        "reconnects": max(reconnects, 0),
        "bytes": out.stat().st_size,
        "completeness": checker.report()["total"],
    }


def _logs_to_stderr() -> None:
    """Keep stdout for the JSON result; structlog prints to stdout by default."""
    import sys

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    p.add_argument("--duration", type=float, default=3600, help="seconds to record (default 3600)")
    p.add_argument("--out", type=Path, default=None, help="output .jsonl.gz path")
    p.add_argument("--progress-every", type=float, default=30, help="seconds between status lines")
    args = p.parse_args(argv)
    _logs_to_stderr()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    out = args.out or Path("recordings") / (
        f"binance-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{int(args.duration)}s.jsonl.gz"
    )
    summary = asyncio.run(record(symbols, args.duration, out, args.progress_every))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
