#  Crypto Alert API

> Real-time cryptocurrency price alerts delivered as signed webhooks

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Built for:** Portfolio project targeting Quantitative Data Engineer roles at top trading firms (Citadel, Jane Street, HRT, etc.)

## 📋 Overview

Crypto Alert API streams live trades from Binance, matches them against
user-defined price alerts, and sends a signed webhook when an alert's condition
is met. Developers get price alerts for bots and notifications without running
their own exchange WebSocket connections.

### Project status

**Ready to self-host; not yet safe to open to other users.** The full pipeline
(live Binance trade → match → signed webhook) has been tested against the real
Binance feed, and there's a [production Compose setup](#run-in-production).
121 tests pass with 80% coverage.

Before letting anyone else use it, read [Known limitations](#-known-limitations):
SSRF and missing rate limiting are the blockers. The full list of changes and
remaining work is in [CHANGELOG.md](CHANGELOG.md).

### Features

- **Fast alert matching**: Redis sorted sets give O(log n + k) matching per
  trade, instead of scanning every alert.
- **Fires exactly once**: alerts are claimed atomically before firing, so
  concurrent trades can't double-fire an alert.
- **Recurring alerts**: fire once per threshold crossing, then re-arm only after
  the price crosses back by a configurable margin (no spam while the price
  hovers near the threshold).
- **Reliable webhooks**: a pool of concurrent workers; retries with exponential
  backoff and jitter for network errors, 5xx, 408, 425 and 429; other 4xx fail
  fast; HMAC-SHA256 signatures; every attempt is logged per alert.
- **Self-healing price feed**: reconnects however the exchange connection
  ends (including Binance's daily close) and resubscribes every symbol; backs
  off if connections keep dropping; subscriptions are restored on restart.
- **Alert expiry**: optional `expires_at`, checked at fire time and cleaned up
  by a background sweep.
- **Secure API keys**: 256-bit keys stored only as SHA-256 hashes and shown once
  at registration; lookup is a single indexed query.
- **Monitoring**: a readiness check (`/health` returns 503 when the database,
  Redis or a price feed is down), HTTP request metrics for Prometheus,
  structured JSON logs, and an operator-only stats endpoint.
- **Safe production defaults**: a separate production stack, and the app
  refuses to start with a placeholder `SECRET_KEY` or `DEBUG=true`.

**Exchanges:** Binance is the only live source. A Coinbase adapter exists but
is not yet wired up.

## 🏗️ Architecture

```
  Developers ──REST (X-API-Key)──▶ FastAPI ──▶ PostgreSQL
                                     │          (users, alerts, webhook logs)
                                     ▼
                                   Redis
                     ┌─────────────────────────────────┐
                     │ armed:   sorted set per symbol, │
                     │          scored by threshold    │
                     │ re-arm:  fired recurring alerts,│
                     │          scored by re-arm price │
                     └─────────────────────────────────┘
                                     ▲
  Binance WS ──trade──▶ Connection ──┤ ZRANGEBYSCORE + atomic ZREM claim
  (1 connection,        Manager      │
   N symbols)                        ▼
                               Webhook queue ──▶ N workers ──▶ your endpoint
                                     ▲                │ failure
                                     └── retry timer ◀┘ (backoff + jitter)

  Background: expiry sweeper (every ALERT_CLEANUP_INTERVAL_SECONDS)
```

**How a trade becomes a webhook:**
1. One WebSocket per exchange carries every subscribed symbol. It connects in
   the background, so startup never waits on the exchange, and it reconnects
   and resubscribes whenever the connection ends.
2. Each trade runs in its own task with its own DB session.
3. `ZRANGEBYSCORE` finds alerts whose threshold has been crossed, and `ZREM`
   claims each one. Only the task that removes the alert fires it.
4. Expired alerts are retired instead of fired. Recurring alerts move to the
   re-arm set.
5. The webhook is queued. Workers deliver it, and failures retry on timers so
   no worker sits idle waiting for a backoff.

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.11+ (only for running locally or running tests)

### Run with Docker (development)

The default stack hot-reloads code from `./src`. Postgres, Redis and Grafana
use default credentials and are bound to localhost. For a real deployment, see
[Run in production](#run-in-production).

```bash
git clone https://github.com/yourusername/crypto-alert-api.git
cd crypto-alert-api

cp .env.example .env
# Edit .env: set SECRET_KEY and METRICS_TOKEN to strong random values:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d --build
docker logs crypto-alert-api --tail 5    # look for "application_started"
```

Tables are created on startup. If you're upgrading a database created before
the API-key change, run the migration once:

```bash
docker exec -i crypto-alert-postgres psql -U postgres -d cryptoalert \
  < scripts/migrations/001_hash_api_keys.sql
```

### Run in production

[`docker-compose.prod.yml`](docker-compose.prod.yml) is a separate stack: code
baked into the image, no hot reload, `DEBUG` forced off, restart on failure,
Postgres and Redis not published to the host, and Grafana/Prometheus on
localhost only. In `.env`, set:

```bash
SECRET_KEY=...            # python -c 'import secrets; print(secrets.token_urlsafe(48))'
METRICS_TOKEN=...         # same command
POSTGRES_PASSWORD=...     # URL-safe characters (it goes into DATABASE_URL)
GRAFANA_ADMIN_PASSWORD=...
```

```bash
docker compose -f docker-compose.prod.yml up -d --build
curl -i http://localhost:8000/api/v1/health    # 200 = ready, 503 = something's down
```

The app **refuses to start** in production with a placeholder or short
`SECRET_KEY`, or with `DEBUG=true`, and the logs say why. It uses its own
project name, so its containers and volumes never mix with the dev stack's.
Put HTTPS in front of it (e.g. Caddy or nginx) before exposing it.

Run exactly one API process: each one opens its own exchange feed and webhook
queue.

### Run locally (for development)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

docker compose up -d postgres redis      # just the databases
make dev                                  # API with auto-reload on :8000
```

`make` targets use `venv/bin/` automatically, so you don't need to activate the
venv for `make dev`, `make test` or `make lint`.

### Using the API

```bash
# 1. Register. The api_key is shown ONCE; store it now.
curl -X POST http://localhost:8000/api/v1/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
# {"id": 1, "email": "you@example.com", "api_key_prefix": "cak_1a2b3c4d",
#  "is_active": true, "created_at": "...", "api_key": "cak_1a2b3c4d..."}

# 2. Create an alert
curl -X POST http://localhost:8000/api/v1/alerts \
  -H "X-API-Key: cak_..." \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "condition": "price_above",
    "threshold": 100000,
    "webhook_url": "https://your-app.com/webhook",
    "is_recurring": false,
    "expires_at": "2026-12-31T23:59:59Z"
  }'

# 3. List alerts / see delivery attempts
curl http://localhost:8000/api/v1/alerts -H "X-API-Key: cak_..."
curl http://localhost:8000/api/v1/alerts/1/logs -H "X-API-Key: cak_..."

# 4. Health
curl http://localhost:8000/api/v1/health
```

##  API Reference

Interactive docs are at http://localhost:8000/docs and `/redoc` when
`DEBUG=true`. They are disabled otherwise. See [docs/API.md](docs/API.md) for
details.

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/v1/register` | none | Register; returns the API key once |
| POST | `/api/v1/alerts` | API key | Create alert |
| GET | `/api/v1/alerts` | API key | List alerts (`status`, `symbol`, `limit`, `offset`) |
| GET | `/api/v1/alerts/{id}` | API key | Get alert |
| PATCH | `/api/v1/alerts/{id}` | API key | Update threshold, URL, recurrence, expiry; pause (`paused`) or resume (`active`) |
| DELETE | `/api/v1/alerts/{id}` | API key | Delete alert (soft delete) |
| GET | `/api/v1/alerts/{id}/logs` | API key | Webhook delivery attempts |
| GET | `/api/v1/health` | none | Readiness: DB, Redis and exchange feeds. 200 if ready, 503 if not |
| GET | `/api/v1/metrics` | operator token | Platform-wide stats ([below](#operator-stats)) |

### Alert conditions

| Condition | Status |
|---|---|
| `price_above` | ✅ Fires when price ≥ threshold |
| `price_below` | ✅ Fires when price ≤ threshold |
| `price_change_percent` | ❌ Not implemented; rejected with `422` |

Symbols must be pairs Binance currently trades (e.g. `BTCUSDT`). The list is
fetched from Binance at star  tup and refreshed hourly. Unknown symbols get `422`,
and alert creation returns `503` for the few seconds before the first load.

### Alert lifecycle

| Type | Behaviour |
|---|---|
| One-shot (`is_recurring: false`) | Fires once, then status → `triggered` |
| Recurring (`is_recurring: true`) | Fires once per crossing; re-arms after price crosses back by `ALERT_REARM_MARGIN` (default 0.1%) |
| With `expires_at` | Never fires after expiry; status → `expired`. Must be in the future; a timestamp without a timezone is treated as UTC |

### Webhooks

When an alert fires, your endpoint receives a `POST`:

```json
{
  "alert_id": 123,
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 100000.0,
  "current_price": 100012.5,
  "triggered_at": "2026-11-12T20:10:33.348000",
  "signature": "4d9553b3..."
}
```

The same signature is also sent in the `X-Webhook-Signature` header.

**Delivery:** a `2xx` response counts as delivered. Network errors, `5xx`,
`408`, `425` and `429` are retried up to `WEBHOOK_MAX_RETRIES` times with
backoff (about 2s, 4s, 8s by default). Any other `4xx` fails immediately.

**Verifying the signature.** The signature is an HMAC-SHA256 of the body
without the `signature` field, serialised compactly:

```python
import hashlib, hmac, json

def verify_webhook(body: bytes, secret: str) -> bool:
    data = json.loads(body)
    claimed = data.pop("signature")
    canonical = json.dumps(data, separators=(",", ":"))
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, claimed)
```

> ⚠️ `secret` is currently the **server's** `SECRET_KEY`. That's fine when you
> run the server yourself. A multi-user deployment needs a separate secret per
> user; see [Known limitations](#-known-limitations).

### Operator stats

`GET /api/v1/metrics` returns platform-wide counts: active alerts, connections,
alerts triggered and webhooks delivered today, average delivery latency, and
uptime. It takes the operator token, **not** a user API key:

```bash
TOKEN=$(grep '^METRICS_TOKEN=' .env | cut -d= -f2-)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/metrics
```

If `METRICS_TOKEN` isn't set, the endpoint returns `503`.

## 🔍 Monitoring

| What | Where | Notes |
|---|---|---|
| Health | `/api/v1/health` | 200 ready / 503 not ready; point an uptime monitor at it |
| Prometheus exporter | http://localhost:9090/metrics | Dev only (localhost, no auth). In production it's reachable only by Prometheus inside the Compose network |
| Prometheus UI | http://localhost:9091 | Scrapes the exporter every 10s. Localhost only |
| Grafana | http://localhost:3000 | Dev: `admin`/`admin`. Production: `GRAFANA_ADMIN_PASSWORD`. Localhost only. **No dashboards provisioned yet** |
| Logs | `docker logs <api container>` | Structured JSON, filtered by `LOG_LEVEL` |

Production binds the UIs to localhost on the server. Reach them through an SSH
tunnel, e.g. `ssh -L 3000:localhost:3000 your-server`.

**Recorded metrics:** `http_requests_total` and `http_request_duration_seconds`,
labelled by method, route template and status. Matching, webhook and exchange
metrics are defined in `src/monitoring/metrics.py` but **not yet recorded**.

## 🧪 Testing

Tests need Postgres and Redis running (`docker compose up -d postgres redis`),
plus a test database, created once:

```bash
docker exec crypto-alert-postgres psql -U postgres -c "CREATE DATABASE cryptoalert_test;"
```

```bash
make test          # all tests (unit + integration), with coverage
make test-cov      # + HTML coverage report in htmlcov/
make lint          # ruff + mypy

# Load test (set METRICS_TOKEN to include the /metrics task)
make test-load
```

What's covered: alert matching and the re-arm logic, concurrent double-fire
protection, the webhook worker pool, retry policy and backoff, API-key storage
and auth, expiry (unit and Postgres), metrics auth and the exporter,
subscription restore on restart, exchange reconnects (against a real local
WebSocket server), alert input validation (symbols, conditions, status
changes, deleted alerts), health checks, and production settings validation.

## ⚙️ Configuration

All settings are in [`.env.example`](.env.example). The important ones, with
their `.env.example` values:

| Variable | Value | Purpose |
|---|---|---|
| `SECRET_KEY` | placeholder | Signs webhooks. Production requires ≥ 32 chars and not the placeholder |
| `METRICS_TOKEN` | unset | Operator token for `/api/v1/metrics`; unset = endpoint disabled |
| `ENVIRONMENT` | `development` | `production` enforces safe settings at startup |
| `DEBUG` | `true` | Enables `/docs`. Also echoes all SQL and shows error details; refused in production |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every trade; keep `INFO` or above |
| `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | unset | Required by the production stack |
| `MAX_ALERTS_PER_USER` | 100 | Per-user active alert cap |
| `ALERT_REARM_MARGIN` | 0.001 | Recurring re-arm margin (0.1%) |
| `ALERT_CLEANUP_INTERVAL_SECONDS` | 60 | Expiry sweep interval |
| `SYMBOL_REFRESH_SECONDS` | 3600 | How often the tradable-symbol list is refreshed |
| `WEBHOOK_WORKERS` | 10 | Concurrent webhook deliveries |
| `WEBHOOK_MAX_RETRIES` | 3 | Retries per webhook |
| `WEBHOOK_RETRY_DELAY_SECONDS` | 2 | Base backoff (doubles each retry) |

`RATE_LIMIT_*` settings exist but are **not enforced yet**.

## ⚠️ Known limitations

These come from an end-to-end test against the live container. Full list with
details: [CHANGELOG.md → To do](CHANGELOG.md#to-do).

**Must fix before letting other people use it:**
- **SSRF:** webhook URLs aren't restricted, so the server will POST to internal
  addresses (e.g. cloud metadata at `169.254.169.254`).
- Webhooks are signed with the shared server secret; per-user secrets are needed.
- No rate limiting, no API key rotation, and no Alembic migrations.
- No CI yet, and HTTPS is up to you (put a reverse proxy in front).

**Reliability:** keepalive pings catch a dead connection, but not a feed that
stays connected while trades stop arriving. A watchdog for that is planned.

**Scale/durability:** run a single API process (each would open its own feeds
and webhook queue). Pending webhook retries and recurring re-arm state are held
in memory and lost on restart. Performance has not been benchmarked yet.

## 🎓 Interview Talking Points

### 1. Performance
- "Alert matching uses Redis sorted sets: `ZRANGEBYSCORE` is O(log n + k) per
  trade instead of an O(n) scan of every alert."
- "I replaced bcrypt API-key checks (226 ms of CPU per request, blocking the
  event loop) with an indexed SHA-256 lookup (about 6 µs). bcrypt protects
  low-entropy passwords; a 256-bit random key doesn't need it."
- "One WebSocket per exchange carries every symbol, so N alerts share one
  connection."

### 2. Concurrency and correctness
- "Trades are processed concurrently, so two ticks could match the same alert.
  Using `ZREM` as an atomic claim means exactly one task fires it."
- "Recurring alerts use edge triggering with hysteresis. A re-arm set scored by
  re-arm price makes 'what should re-arm now' the same O(log n + k) query."
- "`AsyncSession` isn't safe for concurrent use, so each trade gets its own
  pooled session. Sharing one session was the original bug."

### 3. Reliability
- "Webhook retries run on timers, not inside workers. The original code slept
  inside the only worker, so one dead endpoint stalled every customer and
  could deadlock the queue."
- "The retry policy separates transient failures (5xx, 429, network) from
  permanent ones (404, 401), and adds jitter so a recovering endpoint isn't hit
  by every retry at once."
- "The feed used to die silently after a day. In websockets, a clean close
  (Binance's 24h limit) ends `async for` without an exception, and reconnect
  only lived in the `except`. Now every exit path reconnects except an
  intentional disconnect, and connections that drop straight away back off
  so they can't trip Binance's connection-rate ban."
- "Expiry is enforced in two layers: a fire-time check guarantees correctness,
  and a sweep handles housekeeping."

### 4. Observability
- "Prometheus labels use route templates, not raw paths, to keep label
  cardinality bounded."
- "Operator stats require a separate bearer token, because platform-wide data
  shouldn't be visible to any customer's API key."
- "`/health` used to be permanently 'degraded' because an unused exchange
  counted as down, so it couldn't signal a real outage. Now unused exchanges
  are 'idle', Redis is checked, every probe has a timeout, and failure is a 503
  that monitors can act on."
- "Production fails fast: the settings refuse a placeholder secret or debug
  mode at startup, rather than running insecurely and logging a warning
  nobody reads."

### 5. Trade-offs and what's next
- "Redis vs in-process memory: Redis lets several instances share alert state,
  and the atomic claim still holds across processes. The cost is a network
  round trip; a Lua script could combine match and claim into one call."
- "The webhook queue is in memory: simple and fast, but pending retries are
  lost on restart. The next step is a Redis sorted set scored by next-attempt
  time."

## 🤝 Contributing

This is a portfolio project, but contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

```bash
make format   # black + ruff --fix
make lint     # ruff + mypy
make test
```

## 📄 License

MIT License - see [LICENSE](LICENSE) file

## 🙏 Acknowledgments

Built as part of the **Big O Energy** learning journey - preparing for roles at Jane Street, Citadel, HRT, Jump Trading, and other top trading firms.

Follow the journey:
- **Twitter/X**: [@bigOenergy__](https://twitter.com/bigOenergy__)
- **YouTube**: Coming soon
- **Blog**: [bigoenergy.dev](https://bigoenergy.dev)

## 📞 Contact

Questions or collaboration? Open an issue or reach out via Twitter!

---

**⚡ Built with Big O Energy - Maximum effort, clean execution, relentless curiosity.**
