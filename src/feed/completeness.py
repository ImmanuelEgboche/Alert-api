"""Is the feed complete? Trade-ID gap and duplicate detection.

Binance gives every trade a per-symbol ID that goes up by exactly 1, so:
  - ID jumps from 100 to 105  -> 4 trades missing (a gap)
  - ID seen again              -> duplicate
  - ID lower than the last one, not seen before -> out of order (it fills a
    gap reported earlier, so the missing count goes back down)

Gaps are expected around reconnects (trades during the outage are never
delivered); gaps on a healthy connection mean messages were dropped.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SymbolSequence:
    first_id: int | None = None
    last_id: int | None = None
    received: int = 0
    missing: int = 0           # IDs skipped and never seen
    gap_events: int = 0        # how many separate jumps
    duplicates: int = 0
    out_of_order: int = 0
    largest_gap: int = 0
    _recent: set[int] = field(default_factory=set, repr=False)
    _recent_order: deque[int] = field(default_factory=deque, repr=False)

    @property
    def expected(self) -> int:
        """Trades the exchange made between the first and last ID we saw."""
        if self.first_id is None or self.last_id is None:
            return 0
        return self.last_id - self.first_id + 1


class TradeSequenceChecker:
    """Tracks trade IDs per symbol and classifies each one."""

    # Remember this many recent IDs per symbol to tell a duplicate from a
    # late (out-of-order) arrival. Bounded so a long-running feed stays small.
    RECENT_WINDOW = 10_000

    def __init__(self) -> None:
        self.symbols: dict[str, SymbolSequence] = {}

    def observe(self, symbol: str, trade_id: int) -> tuple[str, int]:
        """Record one trade ID.

        Returns:
            (kind, n): kind is "ok", "gap", "duplicate" or "out_of_order";
            n is the number of missing trades for a gap, else 0.
        """
        seq = self.symbols.setdefault(symbol, SymbolSequence())

        if trade_id in seq._recent:
            seq.duplicates += 1
            return "duplicate", 0

        self._remember(seq, trade_id)
        seq.received += 1

        if seq.last_id is None:
            seq.first_id = seq.last_id = trade_id
            return "ok", 0

        if trade_id == seq.last_id + 1:
            seq.last_id = trade_id
            return "ok", 0

        if trade_id > seq.last_id:
            gap = trade_id - seq.last_id - 1
            seq.missing += gap
            seq.gap_events += 1
            seq.largest_gap = max(seq.largest_gap, gap)
            seq.last_id = trade_id
            return "gap", gap

        # Lower than the last ID and not a recent duplicate: a late arrival
        seq.out_of_order += 1
        if seq.first_id is not None and trade_id < seq.first_id:
            seq.first_id = trade_id  # widens the range; it wasn't counted missing
        else:
            seq.missing = max(0, seq.missing - 1)
        return "out_of_order", 0

    def _remember(self, seq: SymbolSequence, trade_id: int) -> None:
        seq._recent.add(trade_id)
        seq._recent_order.append(trade_id)
        if len(seq._recent_order) > self.RECENT_WINDOW:
            seq._recent.discard(seq._recent_order.popleft())

    def report(self) -> dict[str, Any]:
        """Per-symbol and total counts, JSON-serialisable."""
        per_symbol = {}
        for sym, s in sorted(self.symbols.items()):
            per_symbol[sym] = {
                "received": s.received,
                "expected": s.expected,
                "missing": s.missing,
                "gap_events": s.gap_events,
                "largest_gap": s.largest_gap,
                "duplicates": s.duplicates,
                "out_of_order": s.out_of_order,
                "completeness_pct": _pct(s.expected - s.missing, s.expected),
            }
        expected = sum(v["expected"] for v in per_symbol.values())
        missing = sum(v["missing"] for v in per_symbol.values())
        return {
            "symbols": per_symbol,
            "total": {
                "received": sum(v["received"] for v in per_symbol.values()),
                "expected": expected,
                "missing": missing,
                "gap_events": sum(v["gap_events"] for v in per_symbol.values()),
                "duplicates": sum(v["duplicates"] for v in per_symbol.values()),
                "out_of_order": sum(v["out_of_order"] for v in per_symbol.values()),
                "completeness_pct": _pct(expected - missing, expected),
            },
        }


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 4) if whole else 100.0
