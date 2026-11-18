"""Per-stage pipeline latency: where does the time go between a trade and its webhook?

Stages (each measured per trade, or per fired alert for the webhook stages):

    exchange_to_receive     exchange trade time -> message reached us.
                            Exchange + network delay. Uses two machines' wall
                            clocks, so it includes clock offset (NTP: ~ms).
    receive_to_match_start  message reached us -> matcher starts on it.
                            Parsing plus time waiting for the event loop.
                            This is the one that grows when we fall behind.
    match                   matcher run for one trade (Redis lookups; for
                            fired alerts also the DB update and enqueue).
    webhook_queue_wait      alert fired -> a delivery worker picks it up.
    webhook_http            HTTP POST to the user's endpoint -> response.
    receive_to_delivered    message reached us -> webhook delivered.
                            Everything this service controls, end to end.
    exchange_to_delivered   exchange trade time -> webhook delivered
                            (exchange_to_receive + receive_to_delivered).

All internal stages use time.perf_counter_ns(), so wall-clock adjustments
can't distort them. Samples are kept in a bounded buffer per stage (the most
recent N), so a long-running server doesn't grow without limit; benchmarks
create their own unbounded recorder.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

STAGES = (
    "exchange_to_receive",
    "receive_to_match_start",
    "match",
    "webhook_queue_wait",
    "webhook_http",
    "receive_to_delivered",
    "exchange_to_delivered",
)

PERCENTILES = (50, 90, 99, 99.9)


class LatencyRecorder:
    """Collects nanosecond samples per stage and summarises them."""

    def __init__(self, max_samples: int | None = 100_000) -> None:
        self.max_samples = max_samples
        self._samples: dict[str, deque[int]] = {}
        self._counts: dict[str, int] = {}

    def record(self, stage: str, ns: int) -> None:
        buf = self._samples.get(stage)
        if buf is None:
            buf = self._samples[stage] = deque(maxlen=self.max_samples)
            self._counts[stage] = 0
        buf.append(ns)
        self._counts[stage] += 1

    def reset(self) -> None:
        self._samples.clear()
        self._counts.clear()

    def summary(self) -> dict[str, dict[str, Any]]:
        """{stage: {count, mean_ms, p50_ms, p90_ms, p99_ms, p99.9_ms, max_ms}}.

        `count` is every sample ever recorded; percentiles cover the samples
        still in the buffer (all of them for an unbounded recorder).
        """
        out: dict[str, dict[str, Any]] = {}
        ordered = [s for s in STAGES if s in self._samples]
        ordered += sorted(s for s in self._samples if s not in STAGES)
        for stage in ordered:
            data = sorted(self._samples[stage])
            if not data:
                continue
            row: dict[str, Any] = {
                "count": self._counts[stage],
                "mean_ms": _ms(sum(data) / len(data)),
            }
            for p in PERCENTILES:
                row[f"p{p:g}_ms"] = _ms(percentile(data, p))
            row["max_ms"] = _ms(data[-1])
            out[stage] = row
        return out


def percentile(sorted_data: list[int], p: float) -> float:
    """Nearest-rank percentile of already-sorted data."""
    if not sorted_data:
        return math.nan
    rank = max(1, math.ceil(p / 100 * len(sorted_data)))
    return float(sorted_data[min(rank, len(sorted_data)) - 1])


def _ms(ns: float) -> float:
    return round(ns / 1e6, 3)


# The live pipeline records here; GET /api/v1/metrics/latency reads it.
pipeline_latency = LatencyRecorder()
