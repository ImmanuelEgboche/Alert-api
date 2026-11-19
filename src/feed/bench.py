"""Benchmark: replay a recording through the real pipeline and measure it.

    python -m src.feed.bench recordings/binance-...jsonl.gz --speed 0 --alerts 1000

What runs is the production code path, end to end:

    ReplayConnection -> ConnectionManager -> AlertMatcher (Redis)
        -> Postgres (trigger update, webhook log, signing-secret lookup)
        -> WebhookDelivery (httpx, HMAC) -> a local HTTP receiver

It uses its OWN database (cryptoalert_bench, created and migrated if missing)
and Redis DB 15, both wiped at the start, so it never touches dev data.

Alerts are generated from the recording itself (seeded, so runs are
repeatable): for each one a symbol is picked, then a threshold between the
symbol's opening price and its high (price_above) or low (price_below) over
the recording - so every alert starts un-triggered and fires only if the
recorded market actually crosses it, like a real user's alert.

Results go to stdout (a table) and, with --out, to a JSON file that includes
the machine, Python version and git commit, so numbers can be traced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/cryptoalert_bench"
DEFAULT_REDIS = "redis://localhost:6379/15"


# --- environment: must be set before any src.* module reads settings ---------

def configure_env(args: argparse.Namespace) -> None:
    os.environ.update({
        "DATABASE_URL": args.database_url,
        "REDIS_URL": args.redis_url,
        "ENVIRONMENT": "development",
        "DEBUG": "false",                      # echo=True would log every SQL statement
        "FEED_SOURCE": "live",                 # the bench builds its own replay feed
        "WEBHOOK_ALLOW_PRIVATE_URLS": "true",  # the receiver is on 127.0.0.1
        "WEBHOOK_WORKERS": str(args.webhook_workers),
        "WEBHOOK_MAX_RETRIES": "0",            # a failed delivery should show, not hide in retries
        "MAX_ALERTS_PER_USER": str(max(args.alerts, 1)),
    })


# --- log capture: count warnings/errors instead of printing thousands -------

class LogCounter:
    """structlog processor: counts warning+ events, prints the first few of each."""

    SHOW_PER_EVENT = 3

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        import structlog

        key = f"{method}:{event_dict.get('event')}"
        self.counts[key] += 1
        if self.counts[key] > self.SHOW_PER_EVENT:
            raise structlog.DropEvent
        return event_dict


# --- local webhook receiver --------------------------------------------------

class Receiver:
    """Minimal HTTP/1.1 keep-alive server that answers 200 to every POST.

    Deliberately trivial so the numbers measure our side, not the receiver.
    """

    def __init__(self) -> None:
        self.received = 0
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/hook"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length:
                    await reader.readexactly(length)
                self.received += 1
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


# --- setup -------------------------------------------------------------------

async def ensure_database(url: str) -> None:
    """Create the bench database if it doesn't exist, then migrate it."""
    import asyncpg
    from sqlalchemy.engine import make_url

    from src.migrate import upgrade_async

    u = make_url(url)
    conn = await asyncpg.connect(
        user=u.username, password=u.password, host=u.host, port=u.port, database="postgres"
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", u.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{u.database}"')
    finally:
        await conn.close()
    await upgrade_async(url)


async def reset_state() -> None:
    from sqlalchemy import text

    from src.database import AsyncSessionLocal, get_redis

    async with AsyncSessionLocal() as s:
        await s.execute(text("TRUNCATE webhook_logs, alerts, users RESTART IDENTITY CASCADE"))
        await s.commit()
    redis = await get_redis()
    await redis.flushdb()


def price_ranges(path: Path) -> dict[str, tuple[float, float, float]]:
    """{symbol: (open, low, high)} over the recording, via the real parser."""
    import json as _json

    from src.feed.recording import read_messages

    ranges: dict[str, list[float]] = {}
    for rec in read_messages(path):
        if '"e":"trade"' not in rec.raw:
            continue
        d = _json.loads(rec.raw)
        d = d.get("data", d)
        p = float(d["p"])
        r = ranges.get(d["s"])
        if r is None:
            ranges[d["s"]] = [p, p, p]
        else:
            r[1] = min(r[1], p)
            r[2] = max(r[2], p)
    return {s: (r[0], r[1], r[2]) for s, r in ranges.items()}


async def seed_alerts(
    n: int, ranges: dict[str, tuple[float, float, float]], webhook_url: str,
    recurring_fraction: float, seed: int,
) -> int:
    """One user, n alerts that start un-triggered (see module docstring)."""
    from src.database import AsyncSessionLocal
    from src.models import Alert, AlertStatus, User

    rng = random.Random(seed)
    symbols = sorted(ranges)
    async with AsyncSessionLocal() as s:
        user = User(email="bench@example.com", api_key_hash="bench", api_key_prefix="bench")
        s.add(user)
        await s.flush()
        alerts = []
        for _ in range(n):
            sym = rng.choice(symbols)
            open_, low, high = ranges[sym]
            if rng.random() < 0.5 and high > open_:
                cond, thr = "price_above", rng.uniform(open_, high)
            elif low < open_:
                cond, thr = "price_below", rng.uniform(low, open_)
            else:
                cond, thr = "price_above", open_ * 1.5  # flat symbol: never fires
            if thr == open_:
                continue
            alerts.append(Alert(
                user_id=user.id, symbol=sym, condition=cond, threshold=thr,
                webhook_url=webhook_url, is_recurring=rng.random() < recurring_fraction,
                status=AlertStatus.ACTIVE.value,
            ))
        s.add_all(alerts)
        await s.commit()
        return len(alerts)


def environment() -> dict[str, Any]:
    def run(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    cpu = run("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor()
    return {
        "machine": f"{cpu} ({os.cpu_count()} cores), {platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "git_commit": run("git", "rev-parse", "--short", "HEAD"),
        "git_dirty": bool(run("git", "status", "--porcelain", "--untracked-files=no")),
    }


# --- the run -------------------------------------------------------------------

async def run_bench(args: argparse.Namespace, log_counter: LogCounter) -> dict[str, Any]:
    from src.core.connection_manager import ConnectionManager
    from src.core.matcher import AlertMatcher
    from src.database import AsyncSessionLocal, close_db
    from src.feed.completeness import TradeSequenceChecker
    from src.feed.recording import read_header
    from src.feed.replay import ReplayConnection
    from src.monitoring.latency import LatencyRecorder
    from src.webhooks.delivery import WebhookDelivery

    env = environment()  # at the start: the commit that actually ran
    await ensure_database(args.database_url)
    await reset_state()

    receiver = Receiver()
    hook_url = await receiver.start()

    ranges = price_ranges(args.recording)
    seeded = await seed_alerts(args.alerts, ranges, hook_url, args.recurring, args.seed)

    latency = LatencyRecorder(max_samples=None)
    sequence = TradeSequenceChecker()
    matcher = AlertMatcher()
    await matcher.initialize()
    delivery = WebhookDelivery(latency=latency)
    await delivery.initialize()

    fired = 0

    async def count_fired(alert_data: Any, price: float, ts: Any, **kw: Any) -> None:
        nonlocal fired
        fired += 1

    matcher.add_trigger_callback(count_fired)
    matcher.add_trigger_callback(delivery.queue_webhook)
    manager = ConnectionManager(matcher, latency=latency, sequence=sequence)

    async with AsyncSessionLocal() as s:
        loaded = await matcher.load_active_alerts(s)

    feed = ReplayConnection(args.recording, symbols=None, speed=args.speed)
    manager.attach_feed(feed)

    peak_pending = 0
    start = time.perf_counter()
    await feed.connect()
    while not feed.finished.is_set():
        peak_pending = max(peak_pending, manager.pending_matches)
        await asyncio.sleep(0.05)
    replay_done = time.perf_counter()

    # Drain: every match task finished, every fired alert delivered (or timed out)
    deadline = time.perf_counter() + args.drain_timeout
    while time.perf_counter() < deadline:
        if not manager.pending_matches and delivery.delivery_queue.empty() \
                and receiver.received >= fired:
            break
        peak_pending = max(peak_pending, manager.pending_matches)
        await asyncio.sleep(0.05)
    drained = time.perf_counter()
    drain_timed_out = bool(manager.pending_matches) or receiver.received < fired

    await delivery.shutdown()
    await manager.disconnect_all()
    await feed.disconnect()
    await receiver.stop()
    await close_db()

    stats = feed.stats
    total_wall = drained - start
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recording": {
            "file": str(args.recording),
            "header": read_header(args.recording),
            "recorded_seconds": round(stats.recorded_seconds, 1),
            "messages": stats.messages,
            "trades": stats.trades,
            "recorded_trades_per_second": round(stats.trades / stats.recorded_seconds, 1)
            if stats.recorded_seconds else None,
        },
        "config": {
            "speed": args.speed or "max",
            "alerts_requested": args.alerts,
            "alerts_loaded": loaded,
            "alerts_seeded": seeded,
            "recurring_fraction": args.recurring,
            "seed": args.seed,
            "webhook_workers": args.webhook_workers,
        },
        "environment": env,
        "throughput": {
            "replay_wall_seconds": round(replay_done - start, 3),
            "total_wall_seconds": round(total_wall, 3),
            "trades_per_second": round(stats.trades / total_wall, 1) if total_wall else None,
            "speedup_vs_real_time": round(stats.recorded_seconds / total_wall, 1)
            if total_wall else None,
            "max_replay_lag_ms": round(stats.max_lag_ms, 3),
            "peak_pending_matches": peak_pending,
            "drain_timed_out": drain_timed_out,
        },
        "alerts": {
            "fired": fired,
            "webhooks_received": receiver.received,
        },
        "latency_ms": latency.summary(),
        "completeness": sequence.report(),
        "warnings_and_errors": dict(log_counter.counts),
    }


def render(result: dict[str, Any]) -> str:
    r, t, a = result["recording"], result["throughput"], result["alerts"]
    c = result["completeness"]["total"]
    lines = [
        f"Recording: {r['trades']:,} trades over {r['recorded_seconds']:,}s "
        f"({r['recorded_trades_per_second']}/s live)",
        f"Config:    speed={result['config']['speed']}, "
        f"alerts={result['config']['alerts_loaded']:,}, "
        f"workers={result['config']['webhook_workers']}",
        f"Machine:   {result['environment']['machine']}, Python {result['environment']['python']}",
        "",
        f"Throughput: {t['trades_per_second']:,} trades/s "
        f"({t['speedup_vs_real_time']}x real time), peak in-flight {t['peak_pending_matches']:,}"
        + (" [DRAIN TIMED OUT]" if t["drain_timed_out"] else ""),
        f"Alerts:     {a['fired']:,} fired, {a['webhooks_received']:,} webhooks received",
        f"Feed:       {c['completeness_pct']}% complete, {c['missing']} missing, "
        f"{c['duplicates']} duplicates, {c['gap_events']} gaps",
        "",
        f"{'stage':<24}{'count':>9}{'p50':>10}{'p90':>10}{'p99':>10}{'p99.9':>10}{'max':>10}  (ms)",
    ]
    for stage, row in result["latency_ms"].items():
        lines.append(
            f"{stage:<24}{row['count']:>9,}{row['p50_ms']:>10.3f}{row['p90_ms']:>10.3f}"
            f"{row['p99_ms']:>10.3f}{row['p99.9_ms']:>10.3f}{row['max_ms']:>10.3f}"
        )
    if result["warnings_and_errors"]:
        lines += ["", "Warnings/errors: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(result["warnings_and_errors"].items()))]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("recording", type=Path)
    p.add_argument("--speed", type=float, default=0,
                   help="0 = as fast as possible (default), 1 = real time, N = N x")
    p.add_argument("--alerts", type=int, default=1000)
    p.add_argument("--recurring", type=float, default=0.5, help="fraction of recurring alerts")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--webhook-workers", type=int, default=10)
    p.add_argument("--drain-timeout", type=float, default=120)
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument("--redis-url", default=DEFAULT_REDIS)
    p.add_argument("--out", type=Path, help="write full results as JSON here")
    args = p.parse_args(argv)

    configure_env(args)

    import logging

    import structlog

    counter = LogCounter()
    structlog.configure(
        processors=[counter, structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )

    result = asyncio.run(run_bench(args, counter))
    print(render(result))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
