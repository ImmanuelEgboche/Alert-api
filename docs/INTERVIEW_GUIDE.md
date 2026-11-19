# Interview Guide - Crypto Alert API

**Portfolio Project for Quantitative Data Engineer Roles**

This guide helps you present this project effectively in technical interviews at trading firms (Citadel, Jane Street, HRT, etc.).

## Executive Summary (30 seconds)

> "I built a pipeline that turns live Binance trades into signed webhooks when price alerts trigger, and then measured it properly. I recorded an hour of real trades, 256,000 of them, and replay that through the production code with every stage timed. The first replay showed the pipeline lost 43% of trades under real bursts. I fixed that, then used the per-stage numbers to find and remove the next bottleneck: matching went from 1.3 to 0.48 milliseconds per trade, and throughput from 1,400 to 3,700 trades a second on a laptop, with zero trades lost."

> **Numbers you can quote** all come from [BENCHMARKS.md](BENCHMARKS.md), which
> says how they were measured and what they don't show (one laptop, local
> Postgres/Redis, Python 3.10). Quote them with that context: "on an M2
> laptop, replaying a real hour" is more credible than a bare number.

## Key Technical Highlights

### 1. Performance Optimization

**Problem:** Need to match thousands of alerts against real-time price updates with minimal latency.

**Solution:** Redis sorted sets for O(log n + k) matching complexity.

**Implementation:**
```python
# Store alerts in sorted set, keyed by symbol+condition
# Score = threshold price, Value = alert_id
await redis.zadd(f"symbol:{symbol}:price_above", {alert_id: threshold})

# Match in O(log n): Get all alerts where threshold <= current_price
triggered = await redis.zrangebyscore(symbol_key, "-inf", current_price)
```

**Results:** one Redis round trip per trade; match p50 0.48 ms, p99 1.87 ms,
3,665 trades/s replaying a real hour with 1,000 alerts. Ten times the alerts
(10,000, firing ten times as often) cost 37% of throughput, consistent with
O(log n + k): the cost is in the k alerts that fire (a Postgres write each,
inside the match step), not the n that exist.

**Interview Talking Point:**
"I chose Redis sorted sets over a naive O(n) scan: each trade does one range query, O(log n + k), where k is the number of alerts that actually fire, instead of checking every alert. The trade-off is a network round trip to Redis versus pure in-memory, but several instances can share alert state, and the atomic ZREM claim still prevents double-firing across processes."

---

### 2. WebSocket Connection Pooling

**Problem:** Maintaining thousands of individual WebSocket connections is resource-intensive and rate-limited by exchanges.

**Solution:** Connection multiplexing - share single connection per exchange per symbol across multiple users.

**Architecture:**
```
Before:                After:
1000 alerts           1000 alerts
   ↓                      ↓
1000 WS connections    1 WS connection (multiplexed)
~10MB memory          ~10KB memory
1000 auth requests    1 auth request
```

**Implementation Highlights:**
- Single WebSocket per symbol
- Callback pattern for fan-out to multiple alerts
- Automatic reconnection with exponential backoff
- Connection health monitoring

**Interview Talking Point:**
"WebSocket multiplexing reduced our connection overhead by 1000x. This matters because exchanges rate-limit connections and each WebSocket costs ~10KB + TLS overhead. By sharing connections, we can scale to millions of alerts without hitting exchange limits."

---

### 3. Reliable Webhook Delivery

**Problem:** Webhook endpoints may be temporarily unavailable, but users expect reliable delivery.

**Solution:** Async delivery queue with exponential backoff retry logic.

**Key Features:**
- Non-blocking async delivery (doesn't slow down matching)
- 3 retry attempts with exponential backoff (2s, 4s, 8s)
- Dead letter queue for failures
- Delivery status tracking and metrics
- HMAC signature for verification

**Limits:** the in-memory queue holds up to `MAX_WEBHOOK_QUEUE_SIZE` (10K)
webhooks. With an instant local receiver, queue wait is 0.2 ms p50 and the
HTTP call 1.3 ms p50; a real receiver's response time dominates.

**Interview Talking Point:**
"I implemented webhooks as a separate async queue to decouple delivery from the critical alert matching path. If a webhook endpoint is slow or down, it doesn't affect other users. The exponential backoff prevents thundering herd when services recover, and we track all attempts for debugging."

---

### 4. Data Structures & Algorithms

**Core Algorithm:** Alert Matching Engine

```python
# Pseudocode for price update processing:
def match_price_update(symbol: str, price: float):
    # O(log n) - Binary search in sorted set
    above_alerts = redis.zrangebyscore(
        f"{symbol}:price_above",
        min="-inf",
        max=price  # All thresholds <= price
    )

    below_alerts = redis.zrangebyscore(
        f"{symbol}:price_below",
        min=price,  # All thresholds >= price
        max="+inf"
    )

    # O(k) - Process k triggered alerts
    for alert in above_alerts + below_alerts:
        # O(1) - Hash lookup for alert metadata
        metadata = redis.hget(f"alert:{alert.id}")

        # Async webhook delivery (non-blocking)
        queue_webhook(metadata, price)

    # Total: O(log n + k)
```

**Space Complexity:**
- Redis sorted set: O(n) where n = number of alerts
- Alert metadata: O(n * m) where m = average metadata size (~100 bytes)
- Total: ~100 bytes per alert

**Interview Talking Point:**
"The matching engine is the hot path, so I optimized for read performance. Redis sorted sets give us O(log n) range queries, and we cache alert metadata in hashes for O(1) lookups. This beats alternatives like database scans (O(n)) or maintaining separate interval trees per symbol (O(n) space overhead)."

---

### 5. System Design Decisions

#### Decision 1: Redis vs Pure In-Memory

**Trade-off:**
- **In-Memory**: Lower latency (~0ms), simpler
- **Redis**: Persistence, horizontal scaling, +1ms latency

**Choice:** Redis

**Reasoning:**
"Redis adds a network round trip per match, but gives us shared state across instances and the option to shard by symbol later. For this workload the scaling and correctness benefits outweigh that cost; the next step would be a Lua script to cut the round trips per fired alert from about 3 to 1."

#### Decision 2: Async vs Sync Webhook Delivery

**Trade-off:**
- **Sync**: Immediate confirmation, blocks matching
- **Async**: Non-blocking, eventual consistency

**Choice:** Async with retry

**Reasoning:**
"Webhook delivery should never block the critical path. By queuing webhooks asynchronously, a slow user endpoint doesn't affect others. We track delivery status and retry failures, so users get reliability without sacrificing performance."

#### Decision 3: PostgreSQL vs NoSQL

**Trade-off:**
- **PostgreSQL**: ACID guarantees, relational integrity
- **NoSQL**: Higher throughput, simpler schema

**Choice:** PostgreSQL

**Reasoning:**
"User accounts and alert metadata need strong consistency and relational integrity (alerts belong to users). PostgreSQL gives us ACID guarantees for critical operations like user registration and billing. For hot data (active alerts), we cache in Redis."

---

## Performance Benchmarks

Full method, results and caveats: [BENCHMARKS.md](BENCHMARKS.md). The story in
three steps, each driven by a number:

1. **Record.** One hour of Binance trades, five symbols, 256,382 trades, no
   gaps (trade IDs are sequential, so completeness is counted). Average 72/s,
   but 3,110 in the busiest second and 57 in a single millisecond.
2. **Replay found data loss.** One task per trade exhausted the 50-connection
   Redis pool during bursts. At 10× replay: 111,273 trades (43%) never matched,
   112 alerts never fired. Fix: ordered per-symbol workers with backpressure.
   Zero lost; every run fires the same 1,568 alerts.
3. **Per-stage timing found the next bottleneck.** Per-symbol ordering made
   BTC serial, and `match` was four Redis round trips. Pipelining them: match
   p50 1.30 → 0.48 ms, throughput 1,416 → 3,665 trades/s.

**If asked "how did you measure it?":** stage timestamps on every trade
(monotonic clock for internal stages, exchange timestamp for network delay),
nearest-rank percentiles over every sample, replaying a committed recording
through the same `_dispatch()` and parser the live feed uses, with seeded
alerts so runs are comparable.

---

## Scalability Strategy

### Horizontal Scaling

```
Load Balancer
    ↓
┌────┴────┬────────┬────────┐
API-1   API-2   API-3   API-4  (Stateless)
└────┬────┴────────┴────────┘
     ↓
  Redis Cluster (Shared State)
     ↓
  PostgreSQL (Metadata)
```

**Scaling Limits:** not measured. Today the app must run as one process
(each would open its own exchange feeds and webhook queue).

**Bottlenecks & Solutions:**
1. **Redis becomes bottleneck** → Shard by symbol
2. **Database writes** → Use read replicas for queries
3. **WebSocket connections** → Distribute by exchange

---

## Monitoring & Observability

### Key Metrics

**Latency (Prometheus Histograms):**
```
alert_matching_duration_seconds{quantile="0.99"} 0.008
webhook_delivery_duration_seconds{quantile="0.99"} 0.150
http_request_duration_seconds{quantile="0.99"} 0.045
```

**Throughput (Prometheus Counters):**
```
alerts_triggered_total{symbol="BTCUSDT"} 12543
webhooks_delivered_total{status="success"} 12398
price_updates_processed_total{exchange="binance"} 1567234
```

**Errors (Prometheus Counters):**
```
websocket_reconnections_total{exchange="binance"} 3
webhook_failures_total 145
api_errors_total{status_code="500"} 0
```

### Alerting Rules

```yaml
- alert: HighLatency
  expr: histogram_quantile(0.99, alert_matching_duration_seconds) > 0.050
  for: 5m

- alert: WebSocketDisconnected
  expr: websocket_connections_active == 0
  for: 2m

- alert: HighWebhookFailureRate
  expr: rate(webhook_failures_total[5m]) > 0.10
  for: 5m
```

---

## Code Quality Highlights

### 1. Type Safety
```python
# Fully typed with mypy strict mode
def match_price_update(
    self,
    symbol: str,
    price: float,
    timestamp: datetime,
    session: AsyncSession
) -> list[Alert]:
    ...
```

### 2. Error Handling
```python
# Graceful degradation with retries
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception_type(WebSocketException),
)
async def connect(self) -> None:
    ...
```

### 3. Testing
- Unit tests: 80%+ coverage
- Integration tests: API endpoints
- Load tests: Locust scenarios
- Benchmark suite: Performance regression detection

---

## Lessons Learned

### 1. Averages hide the failure
**Mistake:** The design looked fine for 72 trades/s, the hour's average.
**Learning:** Test with real bursts. Replaying the recording at its real
timing reproduced 57 trades in one millisecond, and that is what broke it.
Measure first, then optimise the stage the numbers point at.

### 2. WebSocket Reliability
**Challenge:** Exchange WebSockets disconnect randomly.
**Solution:** Exponential backoff + connection health monitoring + automatic recovery.

### 3. Webhook Retry Logic
**Challenge:** Simple retry caused thundering herd when services recovered.
**Solution:** Exponential backoff with jitter (half fixed, half random), so retries spread out instead of arriving together.

---

## Interview Questions to Anticipate

### "How would you scale this to 1M alerts?"

**Answer:**
"Three bottlenecks to address:

1. **Redis**: Shard by symbol. Hash symbol to determine Redis node. (Measure per-node capacity first.)

2. **WebSocket Connections**: Already multiplexed. Can scale to millions with current architecture.

3. **Database**: Use read replicas for queries, async replication for writes. Move to time-series DB for webhook logs.

I'd benchmark one node before estimating cost."

### "What if Redis goes down?"

**Answer:**
"Two strategies:

1. **High Availability**: Redis Sentinel with automatic failover. 3-node cluster with replication. Recovery time: ~2 seconds.

2. **Fallback Mode**: On Redis failure, load active alerts from PostgreSQL into in-memory sorted lists. Slower matching, but the system keeps running.

Trade-off: HA costs more but maintains SLA. Fallback is cheaper but degrades performance."

### "How do you prevent alert abuse?"

**Answer:**
"Multi-layer rate limiting:

1. **Per-user alert cap**: 100 active alerts per user, configurable.
2. **API rate limiting**: 100 requests/min per user, fixed window in Redis so every instance shares the count. Invalid keys don't count against a user, so nobody can lock someone else out.
3. **Sign-up limiting**: 5 registrations/hour per IP.
4. **SSRF protection**: webhook URLs can't target private, loopback or metadata addresses; checked at connect time to beat DNS rebinding.
5. **Authentication**: 256-bit API keys stored as SHA-256 hashes (plaintext shown once, never stored); O(1) indexed lookup; rotatable.

Not built yet: per-endpoint webhook rate limits, and disabling endpoints that keep failing.

For production, add usage-based pricing to naturally limit abuse."

### "What's your p99 latency and why does it matter?"

**Answer:**
"Replaying a real hour at its recorded timing on an M2 laptop, a webhook is delivered 40 ms after the trade reaches us at p50 and 254 ms at p99; from Binance's own trade timestamp it's 114 and 387 ms, about 58 ms of which (median) is the network to Binance. Matching itself is under a millisecond. The p99 is queueing: 84% of trades arrive while their symbol is trading at 100+ a second, and BTC bursts exceed what one worker can match, about 1,100 a second, so trades wait behind each other. The next fix is per-symbol burst capacity: for one-shot alerts, matching a backed-up burst's high and low in one step is equivalent to matching every trade."

p99 matters more than p50 because alerts fire in exactly the moments the market moves fastest, which are the bursts. The first version had a fine average and lost 43% of trades in those bursts. So I track every stage's p99 and p99.9, and the replay benchmark is how I check a change didn't move the tail."

---

## Repository Structure for Interviews

```
crypto-alert-api/
├── ARCHITECTURE.md          # System design (show first!)
├── README.md               # Quick overview
├── docs/
│   ├── INTERVIEW_GUIDE.md  # This file
│   ├── API.md             # API documentation
│   ├── BENCHMARKS.md      # Measured results and method ⭐
│   └── DEPLOYMENT.md      # Self-hosting
├── src/
│   ├── core/              # Alert matching engine ⭐
│   ├── feed/              # Record, replay, completeness, benchmark ⭐
│   ├── exchanges/         # WebSocket connections
│   ├── webhooks/          # Delivery system
│   └── monitoring/        # Prometheus metrics
├── tests/
│   ├── unit/              # Unit tests
│   ├── integration/       # API tests
│   └── load/              # Performance tests
└── benchmarks/
    ├── data/              # One hour of real Binance trades
    └── results/           # Every benchmark run, as JSON

⭐ = Most important to discuss in interviews
```

---

## What Makes This Project Stand Out

1. **Production-Ready**: Full monitoring, logging, error handling, tests
2. **Measured on real data**: a committed recording, reproducible numbers, and a bug the numbers found
3. **Systems Thinking**: Trade-offs, scaling strategy, reliability patterns
4. **Clean Code**: Type-safe, documented, tested, maintainable
5. **Real-World Problem**: Solves actual developer need (webhook infrastructure)

---

## Quick Demo Script (5 minutes)

```bash
# 1. Show architecture diagram
open ARCHITECTURE.md

# 2. Start system on the recorded hour (no live connection needed)
make setup
FEED_SOURCE=replay REPLAY_FILE=benchmarks/data/binance-1h.jsonl.gz REPLAY_SPEED=10 make dev

# 3. Create alert via API
curl -X POST http://localhost:8000/api/v1/register \
  -d '{"email": "demo@example.com"}'
# Save API key

curl -X POST http://localhost:8000/api/v1/alerts \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "symbol": "BTCUSDT",
    "condition": "price_above",
    "threshold": 50000,
    "webhook_url": "https://webhook.site/unique-id"
  }'

# 4. Show Grafana dashboard
open http://localhost:3000

# 5. Show code: Alert matcher
code src/core/matcher.py

# 6. Run the benchmark (about 70 s at max speed)
make bench f=benchmarks/data/binance-1h.jsonl.gz
open docs/BENCHMARKS.md
```

---

## Final Tips

1. **Lead with impact**: what it does and the hard problems you solved (double-firing, SSRF, the silently dying feed)
2. **Show trade-offs**: Every decision has alternatives
3. **Quantify everything**: Use numbers, not adjectives
4. **Admit limitations**: "This doesn't handle X yet because Y"
5. **Connect to business**: "This enables traders to..."

**Remember:** Interviewers want to see how you think, not just what you built.

Good luck! 🚀
