"""Load testing with Locust.

Run with: locust -f tests/load/locustfile.py --host=http://localhost:8000
"""
import os
import random
from locust import HttpUser, task, between


class CryptoAlertUser(HttpUser):
    """Simulated user for load testing."""

    wait_time = between(1, 3)
    api_key: str | None = None

    def on_start(self):
        """Setup user - register and get API key."""
        response = self.client.post(
            "/api/v1/register",
            json={"email": f"loadtest-{random.randint(0, 100000)}@example.com"},
        )
        if response.status_code == 201:
            self.api_key = response.json()["api_key"]

    @task(3)
    def create_alert(self):
        """Create a new alert."""
        if not self.api_key:
            return

        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
        conditions = ["price_above", "price_below"]

        self.client.post(
            "/api/v1/alerts",
            headers={"X-API-Key": self.api_key},
            json={
                "symbol": random.choice(symbols),
                "condition": random.choice(conditions),
                "threshold": random.uniform(1000, 100000),
                "webhook_url": "https://example.com/webhook",
                "is_recurring": random.choice([True, False]),
            },
        )

    @task(5)
    def list_alerts(self):
        """List user's alerts."""
        if not self.api_key:
            return

        self.client.get(
            "/api/v1/alerts",
            headers={"X-API-Key": self.api_key},
        )

    @task(2)
    def get_metrics(self):
        """Get system metrics (operator endpoint: needs METRICS_TOKEN in the env)."""
        token = os.environ.get("METRICS_TOKEN")
        if not token:
            return

        self.client.get(
            "/api/v1/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )

    @task(1)
    def health_check(self):
        """Check system health."""
        self.client.get("/api/v1/health")


class BenchmarkScenario(HttpUser):
    """Benchmark scenario for measuring peak performance."""

    wait_time = between(0.1, 0.5)
    api_key: str | None = None

    def on_start(self):
        """Setup user."""
        response = self.client.post(
            "/api/v1/register",
            json={"email": f"benchmark-{random.randint(0, 100000)}@example.com"},
        )
        if response.status_code == 201:
            self.api_key = response.json()["api_key"]

    @task
    def rapid_fire_creates(self):
        """Rapidly create alerts to test throughput."""
        if not self.api_key:
            return

        for _ in range(10):
            self.client.post(
                "/api/v1/alerts",
                headers={"X-API-Key": self.api_key},
                json={
                    "symbol": "BTCUSDT",
                    "condition": "price_above",
                    "threshold": random.uniform(40000, 60000),
                    "webhook_url": "https://example.com/webhook",
                },
            )
