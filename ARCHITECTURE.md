# Crypto Alert API - System Architecture

## Overview
Production-grade cryptocurrency alert API designed to showcase systems thinking for quantitative data engineering roles at top trading firms (Citadel, Jane Street, HRT, etc.).

**Target Performance Metrics:**
- Latency: <50ms (p99)
- Uptime: 99.9%
- Throughput: 100K+ alerts/day
- User Target: 50-100 developers in 6 weeks

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
│  │ Interval Tree / Sorted Sets         │ │
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
- Support for Binance, Coinbase Pro, Kraken

**Performance Considerations:**
- Single connection per exchange per symbol pair
- Multiplexing: N alerts → 1 WebSocket connection
- Memory: ~10KB per connection
- Latency: Direct feed = 1-5ms from exchange

### 2. Alert Matching Engine
**Purpose:** Efficiently match incoming price updates against active alerts
**Data Structure:** Interval Tree + Redis Sorted Sets

**Algorithm:**
```python
# For each price update:
# 1. O(log n) lookup in interval tree for triggered alerts
# 2. O(1) Redis read for alert metadata
# 3. O(k) webhook dispatch for k matched alerts
# Total: O(log n + k) per price update
```

**Key Features:**
- Price-above, price-below, price-change-percentage conditions
- Time-based expiration
- One-time vs recurring alerts
- Rate limiting per user

**Scalability:**
- 1M alerts: ~100MB memory (100 bytes per alert)
- 1K price updates/sec: ~10ms processing time
- Horizontal scaling: Shard by symbol

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
- Circuit breaker for failing endpoints

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

### 1. Latency (<50ms)
- **In-memory matching:** Redis sorted sets for hot path
- **Connection pooling:** Reuse HTTP/WS connections
- **Async I/O:** Non-blocking operations throughout
- **Local caching:** LRU cache for frequently accessed data

### 2. Throughput (100K+ alerts/day)
- **Batching:** Group webhook deliveries
- **Multiplexing:** Share WebSocket connections
- **Horizontal scaling:** Stateless API servers
- **Efficient data structures:** Interval trees, sorted sets

### 3. Reliability (99.9% uptime)
- **Health checks:** /health endpoint for load balancers
- **Circuit breakers:** Prevent cascade failures
- **Graceful degradation:** Fallback to polling if WS fails
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
2. **Rate Limiting:** Token bucket algorithm
3. **Input Validation:** Pydantic models
4. **Webhook Verification:** HMAC signatures
5. **DDoS Protection:** Nginx rate limiting
6. **Secrets Management:** Environment variables + Vault

## Testing Strategy

1. **Unit Tests:** 80%+ coverage
2. **Integration Tests:** API endpoints + DB
3. **Load Tests:** k6/Locust (1K req/s)
4. **Chaos Testing:** Random connection drops
5. **Benchmark Suite:** Latency/throughput metrics

## Success Metrics for Portfolio

### Technical Metrics (for CV)
- ✅ P99 latency < 50ms
- ✅ 99.9% uptime over 6 weeks
- ✅ 100K+ alerts processed/day
- ✅ Support 3+ exchanges
- ✅ Horizontal scaling demonstrated

### Product Metrics
- ✅ 50-100 developers using API
- ✅ 500+ GitHub stars
- ✅ Featured in dev newsletters
- ✅ 10+ testimonials/use cases

### Code Quality (for interviews)
- ✅ Clean architecture (SOLID principles)
- ✅ Comprehensive tests
- ✅ Performance benchmarks
- ✅ Production-ready monitoring
- ✅ Clear documentation

## Interview Talking Points

1. **Systems Design:**
   - "Interval tree for O(log n) alert matching"
   - "Redis sorted sets for hot path optimization"
   - "WebSocket multiplexing for efficiency"

2. **Performance:**
   - "Achieved <10ms p99 latency in matching engine"
   - "Handled 10K alerts with <100MB memory"
   - "Optimized WebSocket connections: N alerts → 1 connection"

3. **Reliability:**
   - "Implemented exponential backoff for webhook retries"
   - "Circuit breaker pattern for exchange connections"
   - "Zero-downtime deployment with health checks"

4. **Trade-offs:**
   - "Chose Redis over in-memory for persistence vs pure speed"
   - "Async webhook delivery vs synchronous confirmation"
   - "Horizontal scaling vs vertical for cost optimization"

## Next Steps

1. Build MVP (Week 1-2)
2. Open source + docs (Week 3)
3. Community building (Week 4-6)
4. Iterate based on feedback

This architecture demonstrates production-grade systems thinking while remaining implementable and scalable.
