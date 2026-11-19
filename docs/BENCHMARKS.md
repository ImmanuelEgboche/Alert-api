# Benchmarks

Every number here comes from replaying **one real hour of Binance trades**
through the production code path, and every one can be reproduced from this
repo: the recording is committed, and each result file records the commit,
machine and settings it ran with.

- Recording: [`benchmarks/data/binance-1h.jsonl.gz`](../benchmarks/data/binance-1h.jsonl.gz)
- Raw results: [`benchmarks/results/`](../benchmarks/results/)
- Harness: [`src/feed/bench.py`](../src/feed/bench.py)

## Headline

| | v1: task per trade | v2: ordered workers | v3: + one Redis round trip |
|---|---|---|---|
| Trades lost, real bursts (10× replay) | **111,273 of 256,382 (43%)** | 0 | 0 |
| Trades lost, max-speed replay | 46 | 0 | 0 |
| Max throughput (trades/s) | 4,619\* | 1,416 | **3,665** (51× real time) |
| Match time per trade, p50 / p99 | 4.72 / 13.63 ms | 1.30 / 3.66 ms | **0.48 / 1.87 ms** |
| Queueing before match at 10×, p50 / p99 | 1.3 / 52 ms\* | 139 / 1,476 ms | **28 / 524 ms** |
| Alerts fired (1,000 alerts) | 1,567 / 1,456 | 1,568 | 1,568 |

\* v1 looks fast because it doesn't wait for anything: trades that couldn't
get a Redis connection failed instantly and were never matched.

### At real speed

The hour replayed at its recorded timing (v3, 1,000 alerts, replay within
20 ms of the recorded schedule throughout). All 256,382 trades matched, in
order; 1,568 alerts fired; 1,568 webhooks delivered.

| Stage (ms) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Binance → received (network) | 58.1 | 106.3 | 190.9 | 310.0 | 1,002.7 |
| Received → match starts (queueing) | 14.1 | 61.2 | 209.3 | 351.9 | 475.2 |
| Match | 0.89 | 1.96 | 3.91 | 16.3 | 61.6 |
| Fired → delivery worker | 0.36 | 1.05 | 1.96 | 3.16 | 5.27 |
| Webhook HTTP (local receiver) | 2.09 | 4.72 | 8.48 | 18.4 | 22.6 |
| **Received → webhook delivered** | **40.2** | 102.5 | **253.7** | 360.3 | 458.4 |
| **Binance trade → webhook delivered** | **113.8** | 211.6 | **387.1** | 573.5 | 576.4 |

Matching itself is under 1 ms. **Most of the time this service controls is
queueing in bursts.** See [finding 4](#4-at-real-speed-the-typical-trade-arrives-in-a-burst).

## What was recorded

`make record` (`python -m src.feed.record`) captured Binance's public trade
stream for five symbols, using the app's own `BinanceConnection`, and saved
every message verbatim with the time it arrived.

| | |
|---|---|
| Captured | 2026-11-18, 22:30–23:31 UTC, from a home connection in the UK |
| Symbols | BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT |
| Trades | 256,382 (BTC 113,762 · ETH 86,050 · SOL 23,748 · XRP 19,395 · DOGE 13,427) |
| Completeness | **100%**: no trade-ID gaps, duplicates or out-of-order trades; no reconnects |
| Rate | 72 trades/s on average; **3,110 in the busiest second**, 848 in the busiest 100 ms, 57 in a single millisecond. BTC alone peaked at 2,221 in one second |
| Binance → this machine | p50 58 ms, p90 106 ms, p99 191 ms, max 1,003 ms |
| Size | 2.7 MB gzipped (about 11 bytes per trade) |

The average hides the shape that matters: bursts run 40× the average rate,
and a burst is where a pipeline either keeps up, queues, or loses data.

## How a benchmark runs

```
recording ─▶ ReplayConnection ─▶ ConnectionManager ─▶ AlertMatcher ─▶ Postgres
             (same _dispatch()    (match workers,      (Redis sorted    (trigger update,
              and parser as        trade-ID checks)     sets, claim)     webhook log,
              the live feed)                                   │          secret lookup)
                                                               ▼
                                          WebhookDelivery (httpx, HMAC) ─▶ local receiver
```

- **Same code as production.** Replay feeds raw messages through the base
  class's `_dispatch()` and the real Binance parser, exactly as the WebSocket
  loop does. Everything after that is untouched production code.
- **Its own database.** `cryptoalert_bench` and Redis DB 15, wiped at the start
  of each run, so benchmarks never touch dev data.
- **Realistic alerts, seeded.** 1,000 alerts (or 10,000) spread across the five
  symbols. Each threshold sits between the symbol's opening price and its high
  (`price_above`) or low (`price_below`) over the hour, so every alert starts
  un-triggered and fires only if the recorded market crosses it. Half are
  recurring. Seed 42, so every run creates the same alerts.
- **Speeds.** `0` = as fast as possible (throughput). `1` = the recorded timing
  (latency under real load). `10` = ten times faster, which turns the busiest
  second into 31,000 trades/s: a stress test. Paced replay sends messages that
  arrived in the same millisecond back-to-back, so bursts are reproduced, not
  smoothed.
- **The receiver** is a minimal HTTP server on 127.0.0.1 that answers 200, so
  `webhook_http` measures our client, not a slow endpoint or the internet.

### Stages

| Stage | From → to |
|---|---|
| `exchange_to_receive` | Exchange trade time → message received. Taken from the recording, so it's the same in every run |
| `receive_to_match_start` | Message received → matcher starts on it. Queueing: near zero when keeping up, large when not |
| `match` | One trade through the matcher. For a trade that fires alerts, includes the Postgres update and enqueue |
| `webhook_queue_wait` | Alert fired → delivery worker picks it up |
| `webhook_http` | POST → response |
| `receive_to_delivered` | Received → webhook delivered: everything this service controls |
| `exchange_to_delivered` | Exchange trade time → webhook delivered |

## What the numbers found

### 1. The pipeline lost 43% of trades under real bursts (fixed)

The original design started a task per trade. Each task used a Redis
connection, and redis-py's default pool *raises* `Too many connections` when
its 50 connections are busy rather than waiting. So in any burst of more than
~50 trades (this hour had 57 trades land in a single millisecond) the extra
trades failed and their prices were never matched.

The first replay, a 6-second sample, lost 41 of 210 trades. On the full hour
at 10× speed, v1 lost **111,273 of 256,382 trades (43%)** with up to 3,012
matches in flight, and fired 112 fewer alerts than it should have (1,456
instead of 1,568) because the trades that crossed their thresholds were
dropped.

**Fix (v2):** a fixed pool of match workers (`MATCH_WORKERS`, default 8), each
with a bounded queue. Each symbol always goes to the same worker, so its trades
are matched in order; Redis use is capped at the pool size; and a full queue
makes the feed reader wait instead of dropping anything. Redis also uses a
blocking pool now, so API requests wait for a connection too. Result: **zero
lost trades, and the same 1,568 alerts fire on every run**, because the
outcome no longer depends on scheduling luck.

### 2. Correct, but one busy symbol became the bottleneck (improved)

Pinning a symbol to one worker preserves order, but it serialises that
symbol's trades. With a match at ~1.3 ms, one worker handles ~770 trades/s,
and BTC alone peaked at 2,221 in a second, so bursts queued: at 10×, trades
waited 1.5 s at p99 before matching.

The `match` stage showed why it was slow: every trade made **four sequential
Redis round trips** (re-arm and match lookups for each condition), even when
nothing fired, which is almost every trade.

**Fix (v3):** the four lookups go in one Redis pipeline: one round trip for a
quiet trade (a test pins this). Behaviour is unchanged, including the edge
case where a trade re-arms and fires the same alert. Result: match p50
**1.30 → 0.48 ms**, max throughput **1,416 → 3,665 trades/s**, and p99
queueing at 10× **1,476 → 524 ms**.

### 3. Alerts that exist are cheap; alerts that fire are not

Sorted-set lookups are O(log n + k): *n* alerts in the set barely matter, *k*
alerts firing do, because each fired alert is claimed in Redis and updated in
Postgres **inside the match step**, blocking that symbol's worker.

| Max speed | 1,000 alerts | 10,000 alerts |
|---|---|---|
| Alerts fired | 1,568 | 15,838 |
| v2 throughput | 1,416/s | 1,204/s (−15%) |
| v3 throughput | 3,665/s | 2,327/s (−37%) |
| v3 match p99 / p99.9 | 1.87 / 8.1 ms | 8.77 / 77.6 ms |

Removing the round trips on quiet trades (v3) made firing a bigger share of
the remaining cost, so the same tenfold increase now costs more. That points
straight at the next fix: get the Postgres write off the match path.

### 4. At real speed, the typical trade arrives in a burst

At the recorded timing, matching takes under 1 ms, yet a trade waits 14 ms
(p50) to 209 ms (p99) before it's matched. The recording explains it:

- **84%** of trades arrive while their symbol is trading at ≥100/s
- **40%** at ≥500/s, and **17%** at ≥1,000/s

One worker per symbol matches at most ~1,100 trades/s at this run's 0.89 ms
match time, so the busiest bursts queue, and because most trades *are* in
bursts, even the median trade sees some of that queue. The 72 trades/s average
says the system is idle; trade-weighted, it often isn't.

(Match p50 is 0.89 ms here against 0.48 ms at max speed. A mostly idle
process pays event-loop wake-up and CPU frequency costs that a saturated one
doesn't; this wasn't isolated further.)

## What this doesn't show

- **One machine, one process.** Apple M2 Pro (10 cores), macOS. Postgres 16 and
  Redis 7 in Docker on the same machine, so their round trips are local
  (~0.1 ms). On separate hosts, add the network round trip to every Redis and
  Postgres call.
- **Python 3.10.0** (the local venv); the Docker image uses 3.11, which is
  generally faster.
- **The webhook receiver is local and instant.** Real receivers add their own
  response time and internet latency to `webhook_http`; retries and timeouts
  are tested separately, not benchmarked.
- **Max-speed queueing isn't latency.** At speed 0 the replay deliberately
  outruns matching, so `receive_to_match_start` there is how far behind it
  ran, not what a user would see. Use the real-time run for latency.
- **Clock offset.** `exchange_to_receive` compares Binance's clock with this
  machine's (NTP-synced, typically within a few ms). Internal stages use a
  monotonic clock and aren't affected.
- **One hour, one evening.** A volatile day would have bigger bursts; record
  one and re-run.

## Next steps the numbers point to

1. **Raise per-symbol burst capacity** (finding 4): the real-speed p99 is
   queueing behind BTC bursts. When a symbol's queue backs up, matching the
   backlog's high and low in one step is equivalent for one-shot alerts and
   turns a 500-trade backlog into one match.
2. **Take the Postgres write off the match path** (finding 3): queue the fired
   alert's status update instead of awaiting it while the symbol's worker
   waits. That's the p99.9 match tail.
3. **Lua claim-and-fetch.** Firing still costs a ZREM, HGETALL and (for
   recurring alerts) a ZADD per alert; one script would make it one round trip.
4. **Python 3.11+ and separate hosts** for numbers closer to a real deployment.

## Reproduce

Needs Postgres and Redis running (`docker compose up -d postgres redis`).

```bash
# Inspect the recording: trades, gaps, network delay (parser only)
make inspect f=benchmarks/data/binance-1h.jsonl.gz

# Replay through the pipeline: throughput, then latency at real speed (1 hour)
make bench f=benchmarks/data/binance-1h.jsonl.gz
make bench f=benchmarks/data/binance-1h.jsonl.gz speed=1

# Everything: python -m src.feed.bench --help
python -m src.feed.bench benchmarks/data/binance-1h.jsonl.gz \
    --speed 10 --alerts 10000 --out benchmarks/results/mine.json

# Record your own hour
make record                     # → recordings/binance-<time>-3600s.jsonl.gz
```

To see the v1 data loss yourself, check out `ffd60db` (the last commit before
the fix) and run `--speed 10`.

| Result file | Commit | Code |
|---|---|---|
| `v1-task-per-trade-*.json` | `ffd60db` | Task per trade |
| `v2-ordered-workers-*.json` | `0caf931` | Ordered, bounded workers |
| `v3-pipelined-*.json` | `79b12f2` (max speed, 10×), `a258c7c` (real time, 10k alerts) | + one Redis round trip per trade |
