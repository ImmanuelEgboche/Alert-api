# API Reference

Complete API documentation for Crypto Alert API.

## Base URL

```
Development: http://localhost:8000
Production: https://api.cryptoalert.dev
```

## Authentication

All endpoints (except `/register` and `/health`) require API key authentication, except `/metrics`, which takes an operator token (see Metrics).

**Header:**
```
X-API-Key: your-api-key-here
```

**Example:**
```bash
curl -H "X-API-Key: abc123..." http://localhost:8000/api/v1/alerts
```

## Rate Limits

- **Authenticated requests**: 100 requests/minute per user, across all
  endpoints (`RATE_LIMIT_PER_MINUTE`). Requests with an invalid key don't count.
- **Registration**: 5 per hour per client IP (`REGISTER_RATE_LIMIT_PER_HOUR`).
- Over the limit: `429 Too Many Requests` with headers
  - `Retry-After`: seconds until the window resets
  - `X-RateLimit-Limit`: the limit that was hit
  - `X-RateLimit-Remaining`: `0`

## Endpoints

### User Management

#### Register User

Create new user account. The response is the **only** time the API key and
webhook secret are shown; store both.

**Request:**
```http
POST /api/v1/register
Content-Type: application/json

{
  "email": "user@example.com"
}
```

**Response (201 Created):**
```json
{
  "id": 1,
  "email": "user@example.com",
  "api_key_prefix": "cak_9a7b8c6d",
  "is_active": true,
  "created_at": "2024-01-15T12:00:00Z",
  "api_key": "cak_9a7b8c6d5e4f3a2b1c9d8e7f6a5b4c3d2e1f9a8b7c6d5e4f3a2b1c9d8e7f6a5b",
  "webhook_secret": "whsec_3f2a..."
}
```

**Errors:**
- `400`: Email already registered
- `422`: Invalid email format
- `429`: Too many registrations from this IP

#### Get Account

```http
GET /api/v1/me
X-API-Key: cak_...
```

Returns `id`, `email`, `api_key_prefix`, `is_active`, `created_at`. Never
includes the API key or webhook secret.

#### Rotate API Key

```http
POST /api/v1/me/api-key
X-API-Key: cak_...
```

**Response (200):** `{"api_key": "cak_...", "api_key_prefix": "cak_..."}`.
The key used for this request stops working immediately. A lost key with no
copy can't be recovered.

#### Rotate Webhook Secret

```http
POST /api/v1/me/webhook-secret
X-API-Key: cak_...
```

**Response (200):** `{"webhook_secret": "whsec_..."}`. Every delivery from now
on, including retries of earlier alerts, is signed with the new secret.
Accounts created before v1.1 must call this once to learn their secret.

---

### Alert Management

#### Create Alert

Create a new price alert.

**Request:**
```http
POST /api/v1/alerts
X-API-Key: your-api-key
Content-Type: application/json

{
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 50000.0,
  "webhook_url": "https://your-app.com/webhook",
  "is_recurring": false,
  "expires_at": "2024-12-31T23:59:59Z"  // optional
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| symbol | string | Yes | Binance trading pair, e.g. `BTCUSDT` (case-insensitive; must currently trade on Binance) |
| condition | string | Yes | Alert condition: `price_above`, `price_below` |
| threshold | float | Yes | Price threshold (must be > 0) |
| webhook_url | string (URL) | Yes | Public http(s) endpoint. Private, loopback, link-local and metadata addresses, `localhost`, `.internal`/`.local` and single-label hosts are rejected (`422`) |
| is_recurring | boolean | No | Whether alert recurs (default: false) |
| expires_at | datetime | No | Optional expiration time |

**Response (201 Created):**
```json
{
  "id": 123,
  "user_id": 1,
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 50000.0,
  "webhook_url": "https://your-app.com/webhook",
  "status": "active",
  "is_recurring": false,
  "expires_at": null,
  "triggered_at": null,
  "created_at": "2024-01-15T12:00:00Z",
  "updated_at": "2024-01-15T12:00:00Z"
}
```

**Errors:**
- `400`: Max alerts limit reached
- `401`: Invalid API key
- `422`: Invalid parameters, an unsupported condition, or a symbol Binance
  doesn't trade
- `503`: The symbol list hasn't loaded yet (just after startup); retry after
  the `Retry-After` seconds

---

#### List Alerts

Get list of user's alerts with optional filters.

**Request:**
```http
GET /api/v1/alerts?status=active&symbol=BTCUSDT&limit=50&offset=0
X-API-Key: your-api-key
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| status | string | null | Filter by status: `active`, `paused`, `triggered`, `expired` |
| symbol | string | null | Filter by trading pair |
| limit | integer | 100 | Max results (max: 500) |
| offset | integer | 0 | Pagination offset |

**Response (200 OK):**
```json
[
  {
    "id": 123,
    "user_id": 1,
    "symbol": "BTCUSDT",
    "condition": "price_above",
    "threshold": 50000.0,
    "webhook_url": "https://your-app.com/webhook",
    "status": "active",
    "is_recurring": false,
    "expires_at": null,
    "triggered_at": null,
    "created_at": "2024-01-15T12:00:00Z",
    "updated_at": "2024-01-15T12:00:00Z"
  }
]
```

---

#### Get Alert Details

Retrieve specific alert by ID.

**Request:**
```http
GET /api/v1/alerts/{alert_id}
X-API-Key: your-api-key
```

**Response (200 OK):**
```json
{
  "id": 123,
  "user_id": 1,
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 50000.0,
  "webhook_url": "https://your-app.com/webhook",
  "status": "active",
  "is_recurring": false,
  "expires_at": null,
  "triggered_at": null,
  "created_at": "2024-01-15T12:00:00Z",
  "updated_at": "2024-01-15T12:00:00Z"
}
```

**Errors:**
- `404`: Alert not found
- `401`: Unauthorized

---

#### Update Alert

Modify existing alert parameters.

**Request:**
```http
PATCH /api/v1/alerts/{alert_id}
X-API-Key: your-api-key
Content-Type: application/json

{
  "threshold": 55000.0,
  "webhook_url": "https://new-endpoint.com/webhook",
  "status": "paused"
}
```

**Updateable Fields:**
- `threshold`: New price threshold
- `webhook_url`: New webhook endpoint
- `status`: `active` or `paused` (use `DELETE` to delete). Setting `active`
  re-arms a paused or triggered alert. It counts toward the alert limit (`400`
  if full), and an expired alert also needs a new future `expires_at` (`409`
  otherwise).
- `is_recurring`: Toggle recurring behavior
- `expires_at`: Update expiration time

**Response (200 OK):**
```json
{
  "id": 123,
  "user_id": 1,
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 55000.0,
  "webhook_url": "https://new-endpoint.com/webhook",
  "status": "active",
  "is_recurring": false,
  "expires_at": null,
  "triggered_at": null,
  "created_at": "2024-01-15T12:00:00Z",
  "updated_at": "2024-01-15T12:30:00Z"
}
```

---

#### Delete Alert

Delete an alert (soft delete).

**Request:**
```http
DELETE /api/v1/alerts/{alert_id}
X-API-Key: your-api-key
```

**Response (204 No Content)**

**Errors:**
- `404`: Alert not found
- `401`: Unauthorized

---

#### Get Alert Logs

Retrieve webhook delivery logs for an alert.

**Request:**
```http
GET /api/v1/alerts/{alert_id}/logs?limit=50
X-API-Key: your-api-key
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| limit | integer | 50 | Max results (max: 200) |

**Response (200 OK):**
```json
[
  {
    "id": 456,
    "alert_id": 123,
    "status": "delivered",
    "http_status_code": 200,
    "response_time_ms": 145.2,
    "error_message": null,
    "retry_count": 0,
    "timestamp": "2024-01-15T12:34:56Z"
  },
  {
    "id": 457,
    "alert_id": 123,
    "status": "failed",
    "http_status_code": 500,
    "response_time_ms": 5000.0,
    "error_message": "Connection timeout",
    "retry_count": 3,
    "timestamp": "2024-01-15T13:00:00Z"
  }
]
```

---

### System

#### Health Check

Readiness check: can this instance match prices and fire alerts? Returns
**200** when ready and **503** when a required service is down, so uptime
monitors and load balancers can act on the status code alone.

**Request:**
```http
GET /api/v1/health
```

**Response (200 OK):**
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "timestamp": "2024-01-15T12:00:00Z",
  "services": {
    "database": "healthy",
    "redis": "healthy",
    "binance": "healthy",
    "coinbase": "idle"
  }
}
```

**Overall `status`:** `healthy` (HTTP 200) or `unhealthy` (HTTP 503).

**Service values:**
- `healthy`: working
- `unhealthy`: database or Redis unreachable (each check times out after 2s)
- `disconnected`: an exchange with active alerts has lost its feed (it
  reconnects automatically)
- `idle`: no alerts use this exchange, so no connection is expected. This
  doesn't affect the overall status.

---

#### Metrics

Get platform-wide performance metrics. **Operator-only:** requires the server's
`METRICS_TOKEN` as a bearer token; user API keys are not accepted. If
`METRICS_TOKEN` is not configured the endpoint returns `503` (disabled).

**Request:**
```http
GET /api/v1/metrics
Authorization: Bearer <METRICS_TOKEN>
```

Prometheus-format metrics are served separately on `METRICS_PORT` (default
9090) for scraping; that port must only be reachable on the internal network.

**Response (200 OK):**
```json
{
  "active_alerts": 12543,
  "active_connections": 3,
  "alerts_triggered_today": 3421,
  "webhooks_delivered_today": 3398,
  "avg_latency_ms": 8.5,
  "uptime_seconds": 864000.0
}
```

---

## Webhook Delivery

When an alert triggers, a POST request is sent to your webhook URL.

### Webhook Payload

```json
{
  "alert_id": 123,
  "symbol": "BTCUSDT",
  "condition": "price_above",
  "threshold": 50000.0,
  "current_price": 50125.50,
  "triggered_at": "2024-01-15T12:34:56Z",
  "signature": "a7f8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8"
}
```

### Webhook Signature Verification

Each webhook is signed with HMAC-SHA256 using **your** `webhook_secret` (from
registration, or `POST /api/v1/me/webhook-secret`). The same signature is in
the body's `signature` field and the `X-Webhook-Signature` header.

The signed bytes are the exact request body with its final `"signature"` field
removed. Verify against the **raw body**: parsing and re-serialising the JSON
can change how numbers are written (`0.00001234` → `1.234e-05`) and make valid
webhooks fail.

**Python:**
```python
import hashlib
import hmac
import re

SIGNATURE_FIELD = re.compile(rb',"signature":"([0-9a-f]{64})"}$')

def verify_webhook(body: bytes, secret: str) -> bool:
    match = SIGNATURE_FIELD.search(body)
    if not match:
        return False
    signed = body[: match.start()] + b"}"
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1).decode())

# Usage (FastAPI)
@app.post("/webhook")
async def handle_webhook(request: Request):
    if not verify_webhook(await request.body(), WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")
    # Process webhook...
```

**Node.js** (use the raw body, e.g. `express.raw({ type: 'application/json' })`):
```javascript
const crypto = require('crypto');

function verifyWebhook(rawBody, secret) {
  const body = rawBody.toString('utf8');
  const match = body.match(/,"signature":"([0-9a-f]{64})"}$/);
  if (!match) return false;
  const signed = body.slice(0, match.index) + '}';
  const expected = crypto.createHmac('sha256', secret).update(signed).digest('hex');
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(match[1]));
}
```

### Retry Logic

- **Attempts**: 3 retries
- **Backoff**: Exponential (2s, 4s, 8s)
- **Timeout**: 5 seconds per attempt
- **Success**: HTTP 2xx response
- **Failure**: recorded as `failed` in the alert's logs after max retries
- **Never retried**: most 4xx responses, and URLs that resolve to a non-public
  address (logged as `Blocked: ...`). Redirects are not followed.

### Webhook Best Practices

1. **Respond quickly** (< 5s)
2. **Return 2xx** status code
3. **Verify signature** before processing
4. **Process asynchronously** (queue for background job)
5. **Implement idempotency** (use `alert_id` + `triggered_at`)
6. **Handle retries** gracefully

---

## Error Responses

All errors follow this format:

```json
{
  "error": "Error type",
  "detail": "Detailed error message",
  "timestamp": "2024-01-15T12:00:00Z"
}
```

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 204 | No Content (success, no body) |
| 400 | Bad Request (invalid parameters) |
| 401 | Unauthorized (missing/invalid API key) |
| 403 | Forbidden (account disabled) |
| 404 | Not Found |
| 422 | Validation Error |
| 429 | Rate Limit Exceeded |
| 500 | Internal Server Error |
| 503 | Service Unavailable |

---

## Code Examples

### Python SDK

```python
import httpx

class CryptoAlertClient:
    def __init__(self, api_key: str, base_url: str = "http://localhost:8000"):
        self.api_key = api_key
        self.base_url = base_url
        self.client = httpx.AsyncClient()

    async def create_alert(self, symbol: str, condition: str,
                          threshold: float, webhook_url: str) -> dict:
        response = await self.client.post(
            f"{self.base_url}/api/v1/alerts",
            headers={"X-API-Key": self.api_key},
            json={
                "symbol": symbol,
                "condition": condition,
                "threshold": threshold,
                "webhook_url": webhook_url,
            }
        )
        response.raise_for_status()
        return response.json()

    async def list_alerts(self) -> list:
        response = await self.client.get(
            f"{self.base_url}/api/v1/alerts",
            headers={"X-API-Key": self.api_key}
        )
        response.raise_for_status()
        return response.json()

# Usage
client = CryptoAlertClient("your-api-key")
alert = await client.create_alert(
    "BTCUSDT", "price_above", 50000, "https://example.com/webhook"
)
```

### JavaScript/TypeScript

```typescript
class CryptoAlertClient {
  constructor(
    private apiKey: string,
    private baseUrl: string = 'http://localhost:8000'
  ) {}

  async createAlert(params: {
    symbol: string;
    condition: string;
    threshold: number;
    webhookUrl: string;
  }) {
    const response = await fetch(`${this.baseUrl}/api/v1/alerts`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        symbol: params.symbol,
        condition: params.condition,
        threshold: params.threshold,
        webhook_url: params.webhookUrl,
      }),
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.statusText}`);
    }

    return response.json();
  }

  async listAlerts() {
    const response = await fetch(`${this.baseUrl}/api/v1/alerts`, {
      headers: { 'X-API-Key': this.apiKey },
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.statusText}`);
    }

    return response.json();
  }
}

// Usage
const client = new CryptoAlertClient('your-api-key');
const alert = await client.createAlert({
  symbol: 'BTCUSDT',
  condition: 'price_above',
  threshold: 50000,
  webhookUrl: 'https://example.com/webhook',
});
```

---

## Support

- **Documentation**: https://github.com/yourusername/crypto-alert-api/docs
- **Issues**: https://github.com/yourusername/crypto-alert-api/issues
- **API Status**: https://status.cryptoalert.dev
