"""The record and bench command-line tools, end to end."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import websockets

from src.config import get_settings
from src.feed import record as record_mod
from src.feed.recording import read_header, read_messages
from tests.unit.test_feed import T0, trade_msg, write_recording

ROOT = Path(__file__).resolve().parents[2]


async def test_record_captures_raw_feed_and_reports_gaps(tmp_path, monkeypatch):
    """Records from a local fake Binance that skips trade ID 3."""
    async def fake_binance(ws):
        await ws.recv()  # SUBSCRIBE
        await ws.send('{"result":null,"id":1}')
        for tid in (1, 2, 4, 5):
            await ws.send(trade_msg("BTCUSDT", tid, 100.0 + tid, T0 + tid))
        await ws.wait_closed()

    server = await websockets.serve(fake_binance, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(get_settings(), "binance_ws_url", f"ws://127.0.0.1:{port}")
    out = tmp_path / "rec.jsonl.gz"
    try:
        summary = await record_mod.record(["BTCUSDT"], 0.5, out, progress_every=1)
    finally:
        server.close()
        await server.wait_closed()

    assert summary["trades"] == 4 and summary["messages"] == 5
    assert summary["completeness"]["missing"] == 1
    assert read_header(out)["symbols"] == ["BTCUSDT"]
    raws = [m.raw for m in read_messages(out)]
    assert raws[0] == '{"result":null,"id":1}' and len(raws) == 5   # verbatim


def test_bench_replays_through_the_real_pipeline(tmp_path):
    """Runs the harness as a subprocess (it wipes the database it's given, so
    it gets a throwaway one) on a small recording where prices sweep up and
    down, so seeded alerts cross their thresholds."""
    trades = []
    for i in range(300):
        swing = i if i < 150 else 300 - i
        trades.append(("BTCUSDT", i + 1, 50_000 + swing * 10))
        trades.append(("ETHUSDT", i + 1, 3_000 - swing))
    rec = write_recording(tmp_path / "r.jsonl.gz", trades, gap_ms=1)
    out = tmp_path / "result.json"

    db = get_settings().database_url_str.rsplit("/", 1)[0] + "/cryptoalert_bench_test"
    proc = subprocess.run(
        [sys.executable, "-m", "src.feed.bench", str(rec), "--speed", "0",
         "--alerts", "40", "--database-url", db, "--redis-url", "redis://localhost:6379/15",
         "--drain-timeout", "30", "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Throughput:" in proc.stdout

    result = json.loads(out.read_text())
    assert result["recording"]["trades"] == 600
    assert result["completeness"]["total"]["missing"] == 0
    assert result["throughput"]["drain_timed_out"] is False
    assert result["alerts"]["fired"] > 0
    assert result["alerts"]["webhooks_received"] == result["alerts"]["fired"]
    assert result["latency_ms"]["match"]["count"] == 600
    assert not any(k.startswith("error:") for k in result["warnings_and_errors"])
    assert result["environment"]["git_commit"]


async def test_record_cli_writes_default_path(tmp_path, monkeypatch):
    """main() picks a timestamped path under recordings/ when --out is omitted."""
    seen = {}

    async def fake_record(symbols, duration, out, progress_every):
        seen.update(symbols=symbols, out=out)
        return {"file": str(out)}

    monkeypatch.setattr(record_mod, "record", fake_record)
    monkeypatch.chdir(tmp_path)
    await asyncio.to_thread(record_mod.main, ["--symbols", "btcusdt, ethusdt", "--duration", "5"])
    assert seen["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert seen["out"].parent == Path("recordings") and seen["out"].name.endswith("-5s.jsonl.gz")
