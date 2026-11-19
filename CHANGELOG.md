# Changelog

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
[To do](#to-do) section at the bottom tracks known gaps.

## [Unreleased]

Two parts: **measurement** (record, replay, benchmark; it found and fixed a
data-loss bug) and **Tier 2** (safe to open to other users). Test suite:
121 → 208 passing.

### Measurement: record, replay, benchmark

The project is now evaluated by replaying real recorded trades through the
pipeline rather than by an always-on deployment. Results: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

- **Fixed: trades were silently lost under bursts.** Every trade started its
  own match task; a burst of more than `REDIS_MAX_CONNECTIONS` trades failed
  with `Too many connections` and those prices were never matched, so alerts
  could miss their crossing. *Verified:* replaying the recorded hour at 10×
  lost 111,273 of 256,382 trades (43%) and fired 112 fewer alerts. Matching now
  runs on `MATCH_WORKERS` ordered workers (a symbol always on the same one)
  with bounded queues; a full queue makes the feed reader wait instead of
  dropping trades. Redis uses a blocking pool. Now 0 lost, and every run fires
  the same alerts.
- **Faster matching: one Redis round trip per trade instead of four.** Match
  p50 1.30 → 0.48 ms; max throughput 1,416 → 3,665 trades/s. Behaviour
  unchanged (tested, including re-arm-and-fire at zero margin).
- **Record:** `make record` captures the live Binance feed verbatim to gzip
  JSON Lines. A one-hour recording (5 symbols, 256,382 trades, 0 gaps) is
  committed at `benchmarks/data/binance-1h.jsonl.gz`.
- **Replay:** `ReplayConnection` plays a recording through the same
  `_dispatch()` and parser as the live socket, at real speed, N× or max.
  `FEED_SOURCE=replay` runs the whole app on a recording (refused in
  production).
- **Stage timings:** exchange → receive → match → webhook queue → HTTP →
  delivered, as p50–p99.9, at `GET /api/v1/metrics/pipeline` (operator token).
- **Completeness:** trade-ID gaps, duplicates and reordering counted per
  symbol, logged as `trade_gap` and reported by the same endpoint.
- **Benchmarks:** `make bench` replays a recording through the real matcher,
  Redis, Postgres and webhook delivery (its own database) and writes a JSON
  report with the commit and machine. Results for each version are in
  `benchmarks/results/`.
- Binance trade timestamps are UTC-aware (were naive local time, stored wrong
  in `triggered_at` on a non-UTC server).
- New settings: `FEED_SOURCE`, `REPLAY_FILE`, `REPLAY_SPEED`,
  `MATCH_WORKERS` (must be below `REDIS_MAX_CONNECTIONS`), `MATCH_QUEUE_SIZE`.

### Upgrade steps (existing deployments)

1. **Nothing to run for the database.** Migrations now run on startup. A
   v1.0.0 database is detected, stamped at the baseline and upgraded in place.
2. **Existing users must rotate their webhook secret** to learn it:
   `POST /api/v1/me/webhook-secret`. Until they do, their webhooks are signed
   with a random secret they don't have.
3. **Update webhook receivers** to verify with the user's `webhook_secret`
   (not `SECRET_KEY`), against the raw body (see README).
4. **Webhooks to private addresses now fail** with `Blocked: ...`. For local
   testing against `localhost`, set `WEBHOOK_ALLOW_PRIVATE_URLS=true` (refused
   in production).
5. **Behind a reverse proxy**, set `FORWARDED_ALLOW_IPS` to the proxy's
   address, or every request shares the proxy's per-IP registration limit.
6. Remove `RATE_LIMIT_BURST` from `.env` (no longer used) and optionally set
   `REGISTER_RATE_LIMIT_PER_HOUR` (default 5).

### Security

- **SSRF: webhooks could target internal services.** Alert create/update now
  rejects private, loopback, link-local, metadata and other non-public
  targets with `422`. At delivery, a custom network backend resolves the host,
  refuses if any address is non-public, and connects to the vetted IP itself,
  so DNS rebinding can't swap in an internal address. TLS still verifies the
  real hostname. IPv6 forms that embed IPv4 (mapped, 6to4, Teredo, NAT64) are
  unwrapped and checked. Redirects and environment proxies are disabled.
  *Verified:* `http://169.254.169.254/` and `localtest.me` (→ 127.0.0.1) are
  blocked; valid HTTPS still delivers; bad certificates are still rejected.
- **Per-user webhook signing secrets.** Webhooks were signed with the server's
  `SECRET_KEY`, so users couldn't verify them. Each user now has a `whsec_`
  secret, returned at registration. Delivery reads it on every attempt, so
  rotation applies to pending retries. Disabled users' webhooks aren't sent.
- **Rate limiting.** `RATE_LIMIT_PER_MINUTE` is now enforced per user across
  all authenticated endpoints, and `/register` is limited per IP. Counters
  live in Redis (shared across instances); a Redis outage fails open.
- **API key rotation:** `POST /api/v1/me/api-key`. The old key dies at once.

### Fixed

- **Documented webhook verification failed for low-priced coins.** It
  re-serialised the JSON, turning `0.00001234` into `1.234e-05` and breaking
  the HMAC. Docs now verify the raw body (Python and Node, both tested).

### Added

- `GET /api/v1/me`, `POST /api/v1/me/api-key`, `POST /api/v1/me/webhook-secret`.
- **Alembic migrations** (`alembic/`), applied on startup under a Postgres
  advisory lock; `make migrate` and `make migration m="..."`. A test fails if
  the models and migrations differ.
- **CI** (`.github/workflows/ci.yml`): ruff, the full test suite against
  Postgres and Redis, and a Docker build, on every push and PR.
- Settings: `REGISTER_RATE_LIMIT_PER_HOUR`, `WEBHOOK_ALLOW_PRIVATE_URLS`.

### Changed

- The schema is created by migrations, not `create_all`. setup.sh runs them
  instead of generating a broken Alembic scaffold.
- `SECRET_KEY` no longer signs webhooks (production still requires a strong one).
- Docs no longer claim unmeasured performance (latency, throughput, uptime) or
  features that don't exist (interval trees, circuit breakers, Kraken).

### Removed

- `RATE_LIMIT_BURST` (never used).

## [1.0.0] - 2026-11-18

The alert pipeline (price feed → match → webhook) now works end to end. It was
tested against a live Binance BTCUSDT feed, with a signed webhook received by a
local endpoint. Test suite: 1 → 121 passing, 0 warnings.

### Upgrade steps (existing deployments)

1. **Run the API key migration** before starting the new version:
   `docker exec -i crypto-alert-postgres psql -U postgres -d cryptoalert < scripts/migrations/001_hash_api_keys.sql`
   (idempotent; existing keys keep working).
2. **Set `METRICS_TOKEN`** in `.env`, or `GET /api/v1/metrics` returns 503.
3. **Set `ALERT_CLEANUP_INTERVAL_SECONDS=60`** in `.env` if it still says 3600.
4. **Recreate the dev containers:** `docker compose up -d --build`. This
   rebuilds the API and re-binds Postgres, Redis and Grafana to localhost.
5. **For production**, see "Run in production" in the README: set a real
   `SECRET_KEY` (the app refuses the placeholder), `POSTGRES_PASSWORD` and
   `GRAFANA_ADMIN_PASSWORD`.

### Fixed

- **Webhooks never fired.** The matcher called the webhook callback with 3
  arguments but `queue_webhook` required 4, and the `TypeError` was swallowed as
  `callback_error`. Webhook delivery now opens its own short-lived DB session
  for logging.
- **Concurrent price updates shared one DB session.** Startup took a single
  session from `get_db()` and every trade task reused it, but `AsyncSession` is
  not safe for concurrent use. Each price update now gets its own session, and
  task references are held so tasks can't be garbage-collected mid-run.
- **The Binance connection could never connect.** The base URL had no `/ws`
  path. It is now added automatically.
- **Exchange reconnects gave up permanently** after 5 attempts, and network
  errors (`OSError`, timeouts) were never retried. Retries now cover those
  errors, with exponential backoff capped at 60s and no attempt limit.
- **Recurring alerts fired on every trade** while the price stayed past the
  threshold. They now fire once per crossing and re-arm only after the price
  crosses back by `ALERT_REARM_MARGIN` (default 0.1%), so a price wobbling
  around the threshold can't spam webhooks.
- **Alerts could fire twice** when concurrent ticks matched the same alert.
  Alerts are now claimed atomically with Redis `ZREM` before firing (applies to
  both one-shot and recurring alerts).
- **One failing webhook stalled all deliveries.** Retry backoff slept inside the
  only worker. Retries now run on timers, and delivery uses a worker pool
  (`WEBHOOK_WORKERS`, default 10).
- **Possible queue deadlock.** The worker blocked on putting retries back onto
  its own full queue. Fixed by the retry timers.
- **`expires_at` was ignored.** Expired alerts kept firing and counted toward
  the per-user limit forever. Expiry is now checked at fire time, a background
  sweeper retires due alerts, and startup expires stale alerts before loading.
- **Prometheus scraping was broken.** `prometheus.yml` scraped `api:9090`, but
  nothing listened there. The exporter is now served on `METRICS_PORT`.
- **Metric labels used raw paths** (`/alerts/123`), creating one time series per
  alert ID. Labels now use route templates (`/alerts/{alert_id}`).
- **`uptime_seconds` was always 0.**
- **The Docker container crashed on startup.** `email-validator` (needed by
  `EmailStr`) was missing from `requirements.txt`.
- **The Docker health check always failed.** It imported `requests`, which isn't
  installed. It now uses `urllib`.
- **Alerts stopped working after any restart.** Alerts were reloaded into
  Redis, but exchange subscriptions were not restored, so no prices arrived.
  Startup now re-subscribes the symbols of all active alerts in one batched
  `SUBSCRIBE`. *Verified:* after a container restart Binance reconnects within
  about 1s, where it used to stay `not_connected`.
- **Startup and `POST /alerts` could hang forever while Binance was
  unreachable**, because `connect()` retries without limit and was awaited
  inline. Exchanges now connect in a background task. Symbols added while it
  connects are included in its first `SUBSCRIBE`, and shutdown cancels a
  pending connect.
- **Concurrent subscribes could open duplicate exchange connections**, which
  delivered every trade twice. With connecting moved to the background, the
  check-and-create step has no `await`, so it can't interleave. No lock is
  needed.
- **Resuming a paused alert didn't subscribe its symbol**, so an alert paused
  across a restart stayed silent after `PATCH status: active`.
- **The price feed died after about 24h.** Binance closes every connection
  daily with a clean close (1000/1001). In websockets 12 that ends the
  `async for` read loop without an exception, and reconnecting only happened
  in the `except`, so the feed stopped for good. The read loop now reconnects
  and resubscribes however it ends. Only an intentional `disconnect()` skips
  it, and shutdown cancels a pending reconnect.
- **`/health` reported a dead feed as `healthy`**, because `is_connected` was
  never cleared when the read loop ended.
- **Possible reconnect storm.** Connections that drop within 30s of opening
  now back off (1s, 2s, 4s … 60s), staying under Binance's limit of 300
  connections per 5 minutes per IP. A connection that was stable reconnects
  immediately.
- The read-loop task is now referenced, so it can't be garbage-collected.
- **`PATCH` with `webhook_url` returned 500** and leaked DB details. The
  pydantic `HttpUrl` object was written to a string column. It is now stored
  as a string.
- **Deleted alerts could be resurrected.** Delete is a soft delete, but no route
  filtered `deleted` rows out: they showed in `GET /alerts`, and
  `PATCH status: active` put them back in the matcher. They now return 404
  everywhere.
- **`PATCH` accepted any `status`** (`"banana"`, or `triggered`/`deleted`,
  which only the system or `DELETE` should set). Users can now set only
  `active` or `paused`. Reactivating an expired alert requires a new future
  `expires_at` (409 otherwise).
- **Resuming an alert bypassed the per-user limit.** The limit counts active
  alerts, so pause → create more → resume exceeded it. Reactivation now checks
  the limit too.
- **Alerts that could never fire were accepted.** `price_change_percent` (no
  matching logic) is rejected with 422, and so are symbols Binance doesn't
  trade (`NOTACOIN`) or that aren't alphanumeric (`BTC-USDT`).
- **`/health` always reported `degraded`.** The never-used Coinbase adapter
  counted as down. Exchanges with no alerts now report `idle` and don't count.
  `/health` also checks Redis now (it didn't, though all matching runs there),
  returns **503** when unhealthy so monitors can use the status code, times
  out each check after 2s, and uses `SELECT 1` instead of counting all users.
- **`LOG_LEVEL` was ignored.** structlog had no level filter, so every trade
  logged a debug line (about 10/s for one symbol). It is now applied, and
  validated.
- **`make test` used the global Python** instead of the venv. Make targets now
  use `venv/bin/` when present.
- **46 test deprecation warnings.** Removed the custom `event_loop` fixture, the
  test DB uses `NullPool`, the Redis pool is reset per test, and tests use
  `ASGITransport`. One CPython 3.10.0 bug warning is filtered in `pyproject.toml`.

### Security

- **API keys are no longer stored in plaintext.** They are stored as SHA-256
  hashes with a unique index, and the full key is shown once at registration.
  Keys are prefixed `cak_` so leaked keys are easy to spot. bcrypt (226 ms per
  request, blocking the event loop) is replaced by a 6 µs hash lookup.
- **`GET /api/v1/metrics` now requires an operator token**
  (`Authorization: Bearer <METRICS_TOKEN>`, constant-time compare). User API
  keys are rejected because the stats are platform-wide. If no token is
  configured, the endpoint stays closed.
- **`.env` is no longer baked into the Docker image.** A new `.dockerignore`
  excludes it, and Compose loads it at runtime via `env_file`.
- **The app refuses to start in production with unsafe settings:** a
  placeholder or short (< 32 chars) `SECRET_KEY`, or `DEBUG=true`.
- **Postgres (password `postgres`), Redis (no auth) and Grafana
  (`admin`/`admin`) were published on every network interface.** The dev
  stack now binds them to `127.0.0.1`. The production stack doesn't publish
  Postgres or Redis at all, and requires real passwords.
- **Metrics ports are bound to `127.0.0.1`** in `docker-compose.yml` (the
  exporter and the Prometheus UI).

### Added

- `scripts/migrations/001_hash_api_keys.sql`: plaintext → SHA-256 key migration.
- `src/core/expiry.py`: alert expiry sweeper.
- `docker-compose.prod.yml`: production stack. The image contains the code (no
  mount or reload), `ENVIRONMENT=production` and `DEBUG=false` are forced,
  restart is `unless-stopped`, the API waits for healthy Postgres and Redis,
  images are pinned, and it uses its own project name, separate from dev.
  *Verified:* it refuses to start with the placeholder key, and with a real
  key it serves 200 on `/health` with no debug or SQL logs.
- Settings: `ALERT_REARM_MARGIN`, `WEBHOOK_WORKERS`, `METRICS_TOKEN`,
  `BINANCE_REST_URL`, `SYMBOL_REFRESH_SECONDS`; Compose variables
  `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `API_HOST_PORT` (production).
- `src/exchanges/symbols.py`: Binance trading-symbol list, loaded in the
  background at startup and refreshed hourly. A failed refresh keeps the
  previous list.
- `expires_at` validation: must be in the future; naive datetimes are treated as UTC.
- Webhook retry policy: retry network errors, 5xx, 408, 425 and 429; other 4xx
  responses fail immediately. Backoff includes jitter.
- Tests: recurring alerts and re-arm, concurrent double-fire, webhook worker
  pool and retries, API key storage, expiry (unit + Postgres), metrics auth and
  exporter, restart subscription restore (`tests/integration/test_restart.py`),
  exchange reconnects against a real local WebSocket server
  (`tests/unit/test_exchange_reconnect.py`), alert input validation and the
  symbol registry (`tests/integration/test_alert_validation.py`,
  `tests/unit/test_symbols.py`), health checks (`tests/integration/test_health.py`),
  production settings and log filtering (`tests/unit/test_config.py`).

### Changed

- `POST /register` returns `UserCreatedResponse`, which includes `api_key`.
  Other user responses expose only `api_key_prefix`.
- `ALERT_CLEANUP_INTERVAL_SECONDS` default changed from 3600 to 60.
- `/health`: the overall `status` is `healthy` (200) or `unhealthy` (503).
  Services gain `redis`. Exchange states are `healthy`, `disconnected` or
  `idle`, replacing `degraded`/`not_connected`.
- Dev Compose: Prometheus and Grafana are pinned (v3.16.0, 13.3.1), and the
  obsolete `version:` key is removed.
- `POST /alerts` returns 503 (`Retry-After: 30`) until the symbol list has
  loaded, normally only for a few seconds after startup.
- `PATCH` `status` accepts only `active` or `paused`. `GET /alerts?status=`
  accepts only `active`, `paused`, `triggered` or `expired`.
- `WEBHOOK_RETRY_DELAY_SECONDS` is now a float.
- The Docker health check targets `/`, which checks the process is up, rather
  than `/api/v1/health`.

### Removed

- `passlib` / bcrypt dependency and the `BCRYPT_ROUNDS` setting.
- The plaintext `users.api_key` column.

---

## To do

Found by an end-to-end run against the live container on 2026-11-12.
"Verified" means reproduced; other items come from reading the code.

### Tier 1: reliable for self-hosted use

All Tier 1 items are fixed (see above). Candidate for promotion from "Later":
the stale-feed watchdog.

### Tier 2: safe for other users

Done (see [Unreleased]). Hosting was dropped: the project is evaluated by
replay benchmarks instead of an always-on deployment.

- [x] SSRF protection for webhook URLs
- [x] Per-user webhook signing secrets
- [x] Rate limiting
- [x] API key rotation endpoint
- [x] Alembic migrations
- [x] Own git repo and CI
- [-] ~~HTTPS hosting~~: not pursued. Self-hosters: put a reverse proxy
      with HTTPS in front and set `FORWARDED_ALLOW_IPS`.
- [x] README accuracy: unmeasured claims removed, then replaced with
      measured ones

### Later / nice to have

- [x] Benchmark latency and throughput with a reproducible script
      (`make bench`; 1K and 10K alerts). Still unmeasured: 100K alerts,
      memory per alert.
- [ ] Raise per-symbol burst capacity. At real speed the p99 (254 ms trade
      received → webhook delivered) is queueing behind BTC bursts above
      ~1,100 trades/s, one worker's limit.
- [ ] Move the fired-alert Postgres update off the match path (it is most of
      the match p99.9 tail, and why 10× the alerts costs 37% of throughput).
- [ ] Burst conflation for one-shot alerts (match a backed-up batch's high and
      low instead of every trade).
- [ ] Replay protection: sign a timestamp with each webhook so receivers can
      reject old deliveries.
- [ ] Encrypt webhook secrets at rest (a database leak currently exposes
      them, allowing forged webhooks to users).

- [ ] Durable webhook queue (e.g. a Redis sorted set). Pending retries are lost
      on restart.
- [ ] Persist the re-arm state. A restart re-arms every recurring alert, so it
      may fire once more.
- [ ] A Lua script for atomic claim-and-fetch, cutting the 2-3 Redis round
      trips per *fired* alert to 1 (quiet trades are already 1 round trip).
- [ ] Partial index on `alerts(expires_at) WHERE status IN ('active','paused')`
      for the expiry sweep at scale.
- [ ] Stale-feed watchdog. Keepalive pings detect a dead TCP connection, but
      not one that stays open while the exchange stops sending trades.
      Reconnect if no message arrives for N seconds.
- [ ] Unsubscribe from exchange symbols once no alerts remain.
- [ ] Wire up the Coinbase adapter (implemented, but unreachable: every alert
      uses Binance).
- [ ] Allow clearing `expires_at` via `PATCH` (`null` is currently ignored).
- [ ] Recreate the venv on Python 3.11+ to match Docker and the README; the
      local venv is on 3.10.0.
