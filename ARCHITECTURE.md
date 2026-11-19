# Crypto Alert API - System Architecture

## Overview
Production-grade cryptocurrency alert API designed to showcase systems thinking for quantitative data engineering roles at top trading firms (Citadel, Jane Street, HRT, etc.).

> Performance numbers in this document are measured by replaying a recorded
> hour of real Binance trades through the pipeline; method, caveats and raw
> results are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md). Anything not
> measured there is stated as a complexity or a design goal, not a number.

## System Architecture

```
┌─────────────────┐
│  Client Apps    │
│  (Developers)   │
└────────┬────────┘
         │ REST API
         ▼
┌─────────────────────────────────────────┐
│         API Gateway (FastAPI)           │
│  ┌────────────────────────────────────┐ │
│  │ Auth │ Rate Limit │ Request Valid │ │
│  └────────────────────────────────────┘ │
└──────────────┬──────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌─────────────┐  ┌──────────────┐
│   Redis     │  │  PostgreSQL  │
│   Cache     │  │   Metadata   │
│   State     │  │   Users      │
└─────────────┘  └──────────────┘
        │
        ▼
┌──────────────────────────────────────────┐
│      Alert Matching Engine (Core)        │
│  ┌─────────────────────────────────────┐ │
│  │ Redis Sorted Sets                   │ │
│  │ O(log n + k) matching per price     │ │
│  └─────────────────────────────────────┘ │
└──────────────┬───────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌─────────────┐  ┌──────────────┐
│  WebSocket  │  │   Webhook    │
│  Connection │  │   Delivery   │
│    Pool     │  │    Queue     │
└─────────────┘  └──────────────┘
        │             │
        ▼             ▼
┌─────────────┐  ┌──────────────┐
│  Exchanges  │  │   Client     │
│  (Binance,  │  │  Webhooks    │
│  Coinbase)  │  │              │
└─────────────┘  └──────────────┘
```

## Core Components

### 1. WebSocket Connection Pool
**Purpose:** Maintain persistent connections to crypto exchanges
**Technology:** `websockets` library with connection pooling
**Key Features:**
- Automatic reconnection with exponential backoff
- Connection health monitoring
- Shared connections for multiple alerts (efficiency)
- Binance is live; a Coinbase adapter exists but is not wired up yet

**Performance Considerations:**
- One connection per exchange carries every subscribed symbol: N alerts → 1 WebSocket
- Every raw message goes through one `_dispatch()`, shared by the live socket and
  the replay feed (`src/feed/replay.py`), so recordings exercise the real parser
- Trade IDs are checked per symbol for gaps, duplicates and reordering

### 1b. Match workers
- `MATCH_WORKERS` (default 8) workers with bounded queues; a symbol always maps to
  the same worker, so its trades are matched in order
- A full queue makes the feed reader wait (backpressure) instead of dropping trades.
  The original task-per-trade design lost 43% of trades in a 10× replay of real
  bursts, by exhausting the Redis pool

### 2. Alert Matching Engine
**Purpose:** Efficiently match incoming price updates against active alerts
**Data Structure:** Redis Sorted Sets

**Algorithm:**
```python
# For each price update:
# 1. One pipelined round trip: ZRANGEBYSCORE on the armed and re-arm sets
#    for both conditions, O(log n + k)
# 2. Only for the k alerts that fire: ZREM claim, HGETALL metadata,
#    Postgres status update, enqueue webhook
# Total: O(log n + k) per price update; one Redis round trip when nothing fires
```

**Key Features:**
- Price-above and price-below conditions
- Time-based expiration
- One-time vs recurring alerts (re-arm with hysteresis)

**Measured** (M2 laptop, local Redis/Postgres, 1,000 alerts, real hour replayed):
- Match: p50 0.48 ms, p99 1.87 ms per trade
- 3,665 trades/s sustained (51× the recorded hour's real rate)
- 10,000 alerts instead of 1,000 (10× the firings): 37% less throughput; each
  fired alert's Postgres write runs inside the match step
- At real speed: trade received → webhook delivered p50 40 ms, p99 254 ms
  (mostly queueing in BTC bursts)
- Not measured: memory per alert, more than one process

**Scaling direction:** shard by symbol (each symbol is already pinned to one worker)

### 3. Webhook Delivery System
**Purpose:** Reliable delivery of alert notifications
**Technology:** Background task queue (asyncio)

**Key Features:**
- Retry with exponential backoff (3 attempts)
- Dead letter queue for failed webhooks
- Delivery status tracking
- Webhook signature (HMAC-SHA256)

**Performance:**
- Async delivery: Non-blocking
- Batch processing: Up to 100 webhooks/batch
- Timeout: 5s per webhook
- Non-retryable errors (most 4xx, blocked URLs) fail immediately

### 4. REST API
**Purpose:** User-facing API for CRUD operations
**Technology:** FastAPI (async Python)

**Endpoints:**
```
POST   /api/v1/alerts          - Create alert
GET    /api/v1/alerts          - List user alerts
GET    /api/v1/alerts/{id}     - Get alert details
DELETE /api/v1/alerts/{id}     - Delete alert
GET    /api/v1/alerts/{id}/logs - Get delivery logs
GET    /api/v1/health          - Health check
GET    /api/v1/metrics         - System metrics (admin)
```

**Authentication:** API Key (header: `X-API-Key`)
**Rate Limiting:** 100 req/min per user

### 5. Data Storage

#### PostgreSQL (Metadata)
```sql
tables:
  - users (id, api_key, email, created_at)
  - alerts (id, user_id, symbol, condition, threshold, webhook_url, status)
  - webhook_logs (id, alert_id, timestamp, status, response_time)
  - metrics (timestamp, metric_name, value)
```

#### Redis (Hot State)
```
Keys:
  - alert:{id}           -> Alert metadata (hash)
  - user:{id}:alerts     -> User's alert IDs (set)
  - symbol:{symbol}      -> Active alerts for symbol (sorted set by price)
  - webhook:queue        -> Pending webhooks (list)
  - metrics:*            -> Real-time metrics (strings)
```

## Performance Optimizations

### 1. Latency
- **One Redis round trip per quiet trade:** the four sorted-set lookups are pipelined
  (match p50 1.30 → 0.48 ms when this was added)
- **Connection pooling:** reused HTTP, Redis and Postgres connections
- **Async I/O:** non-blocking throughout
- **Every stage timed:** `/api/v1/metrics/pipeline` reports p50–p99.9 per stage

### 2. Throughput
- **Bounded, ordered match workers** with backpressure instead of unbounded tasks
- **Multiplexing:** one WebSocket per exchange for every symbol
- **Sorted sets:** cost grows with alerts that fire, not alerts that exist

### 3. Reliability
- **Health checks:** /health endpoint for load balancers
- **Timeouts:** every health probe and webhook has one
- **Self-healing feed:** reconnect on every kind of close, with backoff
- **Monitoring:** Prometheus metrics + alerting

## Monitoring & Observability

### Metrics (Prometheus format)
```
# Latency
alert_matching_duration_seconds{quantile="0.99"}
webhook_delivery_duration_seconds{quantile="0.99"}

# Throughput
alerts_triggered_total
webhooks_delivered_total
price_updates_processed_total

# Errors
websocket_reconnections_total
webhook_failures_total
api_errors_total{status_code}

# System
active_alerts_count
active_connections_count
memory_usage_bytes
```

### Logging
- Structured JSON logs (ELK stack compatible)
- Log levels: DEBUG, INFO, WARNING, ERROR
- Correlation IDs for request tracing

## Deployment Architecture

```
┌──────────────────────────────────────┐
│         Load Balancer (Nginx)        │
└──────────────┬───────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌─────────────┐  ┌─────────────┐
│  API Server │  │  API Server │  (Horizontal scaling)
│  (Docker)   │  │  (Docker)   │
└─────────────┘  └─────────────┘
        │             │
        └──────┬──────┘
               ▼
┌──────────────────────────────────────┐
│  Redis Cluster (3 nodes, replication)│
└──────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  PostgreSQL (Primary + Read Replica) │
└──────────────────────────────────────┘
```

### Infrastructure
- **Containers:** Docker + Docker Compose (local), Kubernetes (prod)
- **Cloud:** AWS/GCP (can run on $20/month for demo)
- **CI/CD:** GitHub Actions
- **Monitoring:** Prometheus + Grafana

## Developer Experience

### Quick Start (5 minutes)
```bash
# 1. Get API key
curl -X POST https://api.cryptoalert.dev/register

# 2. Create alert
curl -X POST https://api.cryptoalert.dev/api/v1/alerts \
  -H "X-API-Key: your-key" \
  -d '{
    "symbol": "BTCUSDT",
    "condition": "price_above",
    "threshold": 50000,
    "webhook_url": "https://your-app.com/webhook"
  }'

# 3. Receive webhooks when price > $50k
```

### SDK Support
- Python SDK (reference implementation)
- JavaScript/TypeScript SDK
- OpenAPI spec for code generation

## Security

1. **Authentication:** API keys stored as SHA-256 hashes, looked up by unique index (plaintext returned once at registration)
2. **Rate Limiting:** fixed window per user (and per IP on /register), counted in Redis
3. **Input Validation:** Pydantic models
4. **Webhook Verification:** HMAC signatures
5. **DDoS Protection:** Nginx rate limiting
6. **Secrets Management:** Environment variables + Vault

## Testing Strategy

1. **Unit Tests:** 80%+ coverage
2. **Integration Tests:** API endpoints + DB
3. **Load Tests:** k6/Locust (1K req/s)
4. **Chaos Testing:** Random connection drops
5. **Benchmark Suite:** replay a recorded hour through the pipeline (`make bench`)

## Success Metrics for Portfolio

### Technical Metrics
- [x] Benchmark matching latency and throughput, and publish the method ([BENCHMARKS.md](docs/BENCHMARKS.md))
- [x] Measure feed completeness (trade-ID gaps): 100% over the recorded hour
- [ ] Take the fired-alert Postgres write off the match path (the p99.9 tail)
- [ ] Wire up a second exchange (Coinbase adapter exists)
- [ ] Demonstrate more than one API instance

Not pursued: an always-on public deployment. The project is evaluated by
reproducible replays rather than uptime.

### Code Quality (for interviews)
- ✅ Clean architecture (SOLID principles)
- ✅ Comprehensive tests
- ✅ Performance benchmarks
- ✅ Production-ready monitoring
- ✅ Clear documentation

## Interview Talking Points

1. **Systems Design:**
   - "Redis sorted sets for O(log n + k) alert matching"
   - "WebSocket multiplexing for efficiency"

2. **Performance:**
   - "One WebSocket per exchange carries every symbol: N alerts → 1 connection"
   - "Replaying a real hour: 3,665 trades/s, match p50 0.48 ms, zero trades lost"
   - "Replay found the original design lost 43% of trades in bursts"

3. **Reliability:**
   - "Implemented exponential backoff for webhook retries"
   - "Exchange reconnects with capped exponential backoff, resubscribing every symbol"
   - "Backpressure instead of loss: a full match queue slows the reader"

4. **Trade-offs:**
   - "Chose Redis over in-memory for persistence vs pure speed"
   - "Async webhook delivery vs synchronous confirmation"
   - "Horizontal scaling vs vertical for cost optimization"

## Next Steps

Driven by the benchmark numbers (see [BENCHMARKS.md](docs/BENCHMARKS.md#next-steps-the-numbers-point-to)):

1. Move the fired-alert Postgres update off the match path
2. Lua match-and-claim for alerts that fire
3. Burst conflation for one-shot alerts

This architecture demonstrates production-grade systems thinking while remaining implementable and scalable.
