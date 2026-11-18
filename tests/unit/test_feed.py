"""Record / replay / completeness / latency: the measurement tooling itself."""
import asyncio
import gzip
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core import connection_manager as cm_mod
from src.core.connection_manager import ConnectionManager
from src.exchanges.base import Tick
from src.exchanges.binance import BinanceConnection
from src.feed.completeness import TradeSequenceChecker
from src.feed.recording import (
    RecordingFormatError,
    RecordingWriter,
    read_header,
    read_messages,
)
from src.feed.replay import ReplayConnection, inspect
from src.monitoring.latency import LatencyRecorder, percentile

T0 = 1_700_000_000_000  # ms


def trade_msg(symbol: str, trade_id: int, price: float, t_ms: int) -> str:
    return json.dumps({"e": "trade", "E": t_ms, "s": symbol, "t": trade_id,
                       "p": f"{price:.8f}", "q": "1", "T": t_ms}, separators=(",", ":"))


def write_recording(path, trades, gap_ms: int = 10, latency_ms: int = 50):
    """trades: [(symbol, id, price)]; received latency_ms after each trade."""
    with RecordingWriter(path, "binance", ["BTCUSDT", "ETHUSDT"], "wss://x/ws") as w:
        w.write((T0 - 5) * 1_000_000, '{"result":null,"id":1}')   # subscribe ack
        for i, (sym, tid, price) in enumerate(trades):
            t = T0 + i * gap_ms
            w.write((t + latency_ms) * 1_000_000, trade_msg(sym, tid, price, t))
    return path


# --- recording format ------------------------------------------------------

def test_recording_round_trips_raw_messages(tmp_path):
    path = write_recording(tmp_path / "r.jsonl.gz", [("BTCUSDT", 1, 100.0), ("ETHUSDT", 7, 5.0)])
    assert read_header(path)["exchange"] == "binance"
    msgs = list(read_messages(path))
    assert len(msgs) == 3
    assert msgs[0].raw == '{"result":null,"id":1}'          # kept verbatim, even non-trades
    assert json.loads(msgs[1].raw)["t"] == 1
    assert msgs[1].recv_ns == (T0 + 50) * 1_000_000


def test_truncated_recording_reads_up_to_the_damage(tmp_path):
    """A recorder killed mid-write must still leave a usable file."""
    path = write_recording(tmp_path / "r.jsonl.gz", [("BTCUSDT", i, 1.0) for i in range(1, 200)])
    data = path.read_bytes()
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(data[: len(data) - 40])
    msgs = list(read_messages(cut))
    assert 0 < len(msgs) < 200


def test_rejects_files_that_are_not_recordings(tmp_path):
    bad = tmp_path / "bad.jsonl.gz"
    with gzip.open(bad, "wt") as f:
        f.write('{"hello": 1}\n')
    with pytest.raises(RecordingFormatError):
        read_header(bad)


# --- completeness ------------------------------------------------------------

def test_contiguous_ids_are_complete():
    c = TradeSequenceChecker()
    assert [c.observe("BTC", i)[0] for i in range(100, 105)] == ["ok"] * 5
    total = c.report()["total"]
    assert total["missing"] == 0 and total["completeness_pct"] == 100.0


def test_gap_counts_missing_trades():
    c = TradeSequenceChecker()
    c.observe("BTC", 100)
    assert c.observe("BTC", 105) == ("gap", 4)
    r = c.report()["symbols"]["BTC"]
    assert (r["expected"], r["received"], r["missing"], r["gap_events"]) == (6, 2, 4, 1)
    assert r["completeness_pct"] == pytest.approx(100 * 2 / 6, abs=1e-3)


def test_duplicate_and_late_arrivals():
    c = TradeSequenceChecker()
    for i in (1, 2, 4):
        c.observe("BTC", i)
    assert c.observe("BTC", 4) == ("duplicate", 0)
    assert c.observe("BTC", 3) == ("out_of_order", 0)    # fills the gap
    r = c.report()["symbols"]["BTC"]
    assert (r["missing"], r["duplicates"], r["out_of_order"]) == (0, 1, 1)


def test_symbols_are_tracked_independently():
    c = TradeSequenceChecker()
    c.observe("BTC", 10)
    assert c.observe("ETH", 500) == ("ok", 0)
    assert c.observe("BTC", 11) == ("ok", 0)


# --- latency -----------------------------------------------------------------

def test_percentiles_nearest_rank():
    data = list(range(1, 101))
    assert percentile(data, 50) == 50
    assert percentile(data, 99) == 99
    assert percentile(data, 100) == 100


def test_summary_in_milliseconds_and_bounded_buffer():
    r = LatencyRecorder(max_samples=10)
    for i in range(1, 21):
        r.record("match", i * 1_000_000)  # 1..20 ms
    s = r.summary()["match"]
    assert s["count"] == 20             # every sample counted...
    assert s["max_ms"] == 20.0
    assert s["p50_ms"] == 15.0          # ...but percentiles over the last 10 only


# --- replay --------------------------------------------------------------------

async def collect(conn: ReplayConnection) -> list[Tick]:
    ticks: list[Tick] = []
    conn.add_callback(ticks.append)
    await conn.connect()
    await asyncio.wait_for(conn.finished.wait(), 5)
    return ticks


async def test_replay_uses_the_real_parser_and_keeps_recorded_times(tmp_path):
    path = write_recording(tmp_path / "r.jsonl.gz",
                           [("BTCUSDT", 1, 100.0), ("ETHUSDT", 9, 5.0), ("BTCUSDT", 2, 101.0)])
    ticks = await collect(ReplayConnection(path, speed=0))
    assert [(t.symbol, t.trade_id, t.price) for t in ticks] == [
        ("BTCUSDT", 1, 100.0), ("ETHUSDT", 9, 5.0), ("BTCUSDT", 2, 101.0)]
    assert ticks[0].timestamp == datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    assert ticks[0].recv_ns - ticks[0].exchange_ms * 1_000_000 == 50_000_000  # as recorded
    assert ticks[0].recv_mono_ns > 0


async def test_replay_only_plays_subscribed_symbols(tmp_path):
    path = write_recording(tmp_path / "r.jsonl.gz", [("BTCUSDT", 1, 1.0), ("ETHUSDT", 1, 2.0)])
    conn = ReplayConnection(path, symbols=["ETHUSDT"], speed=0)
    assert [t.symbol for t in await collect(conn)] == ["ETHUSDT"]


@pytest.mark.parametrize("speed,low,high", [(1.0, 0.18, 0.6), (10.0, 0.0, 0.1)])
async def test_replay_speed_keeps_recorded_spacing(tmp_path, speed, low, high):
    """21 trades 10ms apart = 0.2s of market time."""
    path = write_recording(tmp_path / "r.jsonl.gz", [("BTCUSDT", i, 1.0) for i in range(21)])
    conn = ReplayConnection(path, speed=speed)
    start = time.perf_counter()
    await collect(conn)
    assert low <= time.perf_counter() - start <= high
    assert conn.stats.trades == 21


async def test_inspect_reports_gaps_in_a_recording(tmp_path):
    path = write_recording(tmp_path / "r.jsonl.gz",
                           [("BTCUSDT", 1, 1.0), ("BTCUSDT", 2, 1.0), ("BTCUSDT", 6, 1.0)])
    report = await inspect(path)
    assert report["trades"] == 3
    assert report["completeness"]["total"]["missing"] == 3
    assert report["latency"]["exchange_to_receive"]["p50_ms"] == 50.0


async def test_live_and_replay_share_one_dispatch_path(tmp_path):
    """A raw hook (the recorder) sees exactly what replay later feeds back."""
    conn = BinanceConnection("wss://x", ["BTCUSDT"])
    seen: list[str] = []
    conn.add_raw_hook(lambda ns, raw: seen.append(raw))
    ticks: list[Tick] = []
    conn.add_callback(ticks.append)
    msg = trade_msg("BTCUSDT", 42, 123.0, T0)
    await conn._dispatch(msg)
    await conn._dispatch('{"result":null,"id":1}')
    assert seen == [msg, '{"result":null,"id":1}']
    assert [(t.trade_id, t.price) for t in ticks] == [(42, 123.0)]


# --- matching pipeline: ordered, bounded, no drops -------------------------------

class SlowMatcher:
    """Records call order and peak concurrency."""

    def __init__(self, delay: float = 0.001) -> None:
        self.delay = delay
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.peak = 0

    async def match_price_update(self, symbol, price, ts, session, tick=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.calls.append((symbol, tick.trade_id))
        self.active -= 1


@pytest.fixture
def no_db(monkeypatch):
    """Sessions are opened per match; nothing here touches the DB."""
    class _S:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cm_mod, "AsyncSessionLocal", lambda: _S())


async def test_burst_is_bounded_ordered_and_lossless(tmp_path, monkeypatch, no_db):
    """Regression: one task per trade let a burst exceed the Redis pool
    ("Too many connections") and those trades were never matched."""
    monkeypatch.setattr(cm_mod.settings, "match_workers", 4)
    monkeypatch.setattr(cm_mod.settings, "match_queue_size", 5)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    trades = [(syms[i % 3], i // 3 + 1, 1.0) for i in range(300)]
    path = write_recording(tmp_path / "burst.jsonl.gz", trades, gap_ms=0)

    matcher = SlowMatcher()
    manager = ConnectionManager(matcher)
    feed = ReplayConnection(path, speed=0)
    manager.attach_feed(feed)
    await feed.connect()
    await asyncio.wait_for(feed.finished.wait(), 10)
    deadline = time.monotonic() + 5
    while manager.pending_matches and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await manager.disconnect_all()

    assert len(matcher.calls) == 300                       # nothing dropped
    assert matcher.peak <= 4                               # never above the pool size
    for s in syms:                                         # each symbol in trade order
        ids = [tid for sym, tid in matcher.calls if sym == s]
        assert ids == sorted(ids) and len(ids) == 100
    assert manager.sequence.report()["total"]["missing"] == 0
    assert manager.latency.summary()["match"]["count"] >= 300


async def test_replay_feed_is_used_when_configured(tmp_path, monkeypatch):
    path = write_recording(tmp_path / "r.jsonl.gz", [("BTCUSDT", 1, 1.0)])
    monkeypatch.setattr(cm_mod.settings, "feed_source", "replay")
    monkeypatch.setattr(cm_mod.settings, "replay_file", str(path))
    manager = ConnectionManager(MagicMock())
    conn = manager._build_connection("binance", ["BTCUSDT"])
    assert isinstance(conn, ReplayConnection) and conn.symbols == ["BTCUSDT"]
