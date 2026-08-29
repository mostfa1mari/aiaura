"""Data quality layer (Phase 4).

Quality is a DERIVED view over canonical ticks — it never mutates raw data.
Two entry points, sharing ONE per-tick algorithm (``_QualityAccumulator``) so
the batch report and the live monitor cannot diverge:

* ``analyze_ticks(...)``   — batch report over a sequence of ticks.
* ``TickQualityMonitor``   — incremental, safe to use as a provider tick
  listener; keeps running counters and the most recent issues for the live
  monitor / dashboard.

Detected conditions (see docs/DATA_QUALITY.md):
duplicate, out-of-order, gap / abnormal gap, invalid price, future timestamp
(batch only — the live monitor's "now" is the arrival instant), stale, price
spike, plus connection-interruption / reconnection tracking derived from the
provider's connection_events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

# Defaults — deliberately conservative; callers tune per asset/timeframe.
DEFAULT_GAP_S = 5.0            # interval above this is a "gap"
DEFAULT_ABNORMAL_GAP_S = 30.0  # gap above this is "abnormal"
DEFAULT_SPIKE_PCT = 0.005      # 0.5% single-tick move flagged as a spike
DEFAULT_FUTURE_TOLERANCE_S = 2.0  # source_timestamp beyond now+this = future
DEFAULT_STALE_S = 10.0         # no ticks for this long = stale (live monitor)

_SEVERITY = {"info", "warning", "error"}


@dataclass(frozen=True)
class QualityIssue:
    kind: str
    severity: str
    detail: str
    tick_index: Optional[int] = None      # index within the analyzed sequence
    at_timestamp: Optional[float] = None  # source_timestamp of the tick

    def __post_init__(self):
        assert self.severity in _SEVERITY, self.severity


@dataclass
class QualityReport:
    asset: str
    tick_count: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    abnormal_gaps: int = 0
    invalid_prices: int = 0
    future_timestamps: int = 0
    price_spikes: int = 0
    max_gap_s: float = 0.0
    mean_interval_s: float = 0.0
    first_timestamp: Optional[float] = None
    last_timestamp: Optional[float] = None
    issues: List[QualityIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        # Derived from EXACT counters, never the (bounded) issues list — the
        # issue list is a display buffer that may be truncated.
        return (self.invalid_prices + self.out_of_order
                + self.future_timestamps + self.abnormal_gaps)

    @property
    def is_clean(self) -> bool:
        return self.error_count == 0

    @property
    def quality_grade(self) -> str:
        """GOOD / FAIR / POOR — coarse, for dashboards."""
        if self.tick_count == 0:
            return "POOR"
        errs = self.error_count
        if errs == 0:
            return "GOOD"
        if errs <= max(1, self.tick_count // 100):
            return "FAIR"
        return "POOR"

    def summary(self) -> Dict[str, object]:
        return {
            "asset": self.asset,
            "tick_count": self.tick_count,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": self.gaps,
            "abnormal_gaps": self.abnormal_gaps,
            "invalid_prices": self.invalid_prices,
            "future_timestamps": self.future_timestamps,
            "price_spikes": self.price_spikes,
            "max_gap_s": round(self.max_gap_s, 3),
            "mean_interval_s": round(self.mean_interval_s, 4),
            "error_count": self.error_count,
            "grade": self.quality_grade,
        }


def _is_valid_price(price) -> bool:
    return isinstance(price, (int, float)) and math.isfinite(price) and price > 0


class _QualityAccumulator:
    """The single per-tick quality algorithm shared by batch and live paths.

    Feeding the same ticks (same params, same ``now`` policy) through this
    accumulator produces identical counters regardless of entry point.
    """

    def __init__(
        self,
        asset: str,
        *,
        gap_s: float,
        abnormal_gap_s: float,
        spike_pct: float,
        now: Optional[float],
        future_tolerance_s: float,
        issue_limit: int,
        issue_keep: str,  # 'first' (batch) | 'last' (live buffer)
    ) -> None:
        self.report = QualityReport(asset=asset)
        self.gap_s = gap_s
        self.abnormal_gap_s = abnormal_gap_s
        self.spike_pct = spike_pct
        self.now = now
        self.future_tolerance_s = future_tolerance_s
        self.issue_limit = issue_limit
        self.issue_keep = issue_keep
        self._prev = None            # last tick (valid or not) for time continuity
        self._last_valid_price: Optional[float] = None  # spike baseline
        self._interval_sum = 0.0
        self._interval_count = 0

    def _emit(self, issue: QualityIssue) -> None:
        issues = self.report.issues
        if self.issue_keep == "first":
            if len(issues) < self.issue_limit:
                issues.append(issue)
        else:  # 'last' — keep most recent, bounded
            issues.append(issue)
            if len(issues) > self.issue_limit:
                del issues[: -self.issue_limit]

    def add(self, tick, index: Optional[int] = None) -> None:
        r = self.report
        r.tick_count += 1
        if r.first_timestamp is None:
            r.first_timestamp = tick.source_timestamp
        r.last_timestamp = tick.source_timestamp

        if not _is_valid_price(tick.price):
            r.invalid_prices += 1
            self._emit(QualityIssue("invalid_price", "error",
                                    f"non-positive/NaN price {tick.price!r}",
                                    index, tick.source_timestamp))
            # invalid tick still anchors time continuity, but not the spike baseline
            self._prev = tick
            return

        if self.now is not None and tick.source_timestamp > self.now + self.future_tolerance_s:
            r.future_timestamps += 1
            self._emit(QualityIssue("future_timestamp", "error",
                                    f"timestamp {tick.source_timestamp:.3f} > now+{self.future_tolerance_s}",
                                    index, tick.source_timestamp))

        prev = self._prev
        if prev is not None:
            delta = tick.source_timestamp - prev.source_timestamp
            if delta < 0:
                r.out_of_order += 1
                self._emit(QualityIssue("out_of_order", "error",
                                        f"timestamp went backwards by {-delta:.3f}s",
                                        index, tick.source_timestamp))
            else:
                self._interval_sum += delta
                self._interval_count += 1
                if delta > r.max_gap_s:
                    r.max_gap_s = delta
                if delta > self.gap_s:
                    r.gaps += 1
                    if delta > self.abnormal_gap_s:
                        r.abnormal_gaps += 1
                        self._emit(QualityIssue("abnormal_gap", "error",
                                                f"{delta:.3f}s since previous tick",
                                                index, tick.source_timestamp))
                    else:
                        self._emit(QualityIssue("gap", "warning",
                                                f"{delta:.3f}s since previous tick",
                                                index, tick.source_timestamp))
                if delta == 0 and prev.price == tick.price:
                    r.duplicates += 1
                    self._emit(QualityIssue("duplicate", "info",
                                            "identical (timestamp, price) as previous",
                                            index, tick.source_timestamp))

        if self._last_valid_price is not None and self._last_valid_price > 0:
            change = abs(tick.price - self._last_valid_price) / self._last_valid_price
            if change > self.spike_pct:
                r.price_spikes += 1
                self._emit(QualityIssue("price_spike", "warning",
                                        f"{change * 100:.3f}% move in one tick",
                                        index, tick.source_timestamp))

        self._last_valid_price = tick.price
        self._prev = tick
        r.mean_interval_s = (self._interval_sum / self._interval_count) if self._interval_count else 0.0


def analyze_ticks(
    ticks: Sequence,
    *,
    gap_s: float = DEFAULT_GAP_S,
    abnormal_gap_s: float = DEFAULT_ABNORMAL_GAP_S,
    spike_pct: float = DEFAULT_SPIKE_PCT,
    now: Optional[float] = None,
    future_tolerance_s: float = DEFAULT_FUTURE_TOLERANCE_S,
    max_issues: int = 500,
) -> QualityReport:
    """Batch quality report over ``ticks`` (analyzed in given order).

    ``now`` bounds the future-timestamp check; if None, no future check is
    performed (offline analysis of historical captures). Raw data is never
    modified. ``max_issues`` caps the stored issue list (counters are exact).
    """
    asset = ticks[0].asset if ticks else ""
    acc = _QualityAccumulator(
        asset, gap_s=gap_s, abnormal_gap_s=abnormal_gap_s, spike_pct=spike_pct,
        now=now, future_tolerance_s=future_tolerance_s,
        issue_limit=max_issues, issue_keep="first",
    )
    for idx, tick in enumerate(ticks):
        acc.add(tick, index=idx)
    return acc.report


class TickQualityMonitor:
    """Incremental quality tracking. Thread-unsafe by itself; the provider's
    listener dispatch is serialized per tick, so use it as a single listener.

    Uses the same ``_QualityAccumulator`` as ``analyze_ticks`` (with
    ``now=None`` — a live tick's arrival instant is "now", so the future check
    is not meaningful there), guaranteeing identical counters for identical
    tick sequences.
    """

    def __init__(
        self,
        *,
        gap_s: float = DEFAULT_GAP_S,
        abnormal_gap_s: float = DEFAULT_ABNORMAL_GAP_S,
        spike_pct: float = DEFAULT_SPIKE_PCT,
        recent_issue_limit: int = 50,
    ) -> None:
        self.gap_s = gap_s
        self.abnormal_gap_s = abnormal_gap_s
        self.spike_pct = spike_pct
        self._recent_issue_limit = recent_issue_limit
        self._acc: Dict[str, _QualityAccumulator] = {}
        self.reports: Dict[str, QualityReport] = {}

    def __call__(self, tick) -> None:
        self.observe(tick)

    def observe(self, tick) -> None:
        acc = self._acc.get(tick.asset)
        if acc is None:
            acc = _QualityAccumulator(
                tick.asset, gap_s=self.gap_s, abnormal_gap_s=self.abnormal_gap_s,
                spike_pct=self.spike_pct, now=None,
                future_tolerance_s=DEFAULT_FUTURE_TOLERANCE_S,
                issue_limit=self._recent_issue_limit, issue_keep="last",
            )
            self._acc[tick.asset] = acc
            self.reports[tick.asset] = acc.report
        acc.add(tick)

    def report_for(self, asset: str) -> Optional[QualityReport]:
        return self.reports.get(asset)


def summarize_connection_events(events: Sequence[dict]) -> Dict[str, object]:
    """Derive interruption / reconnection stats from provider connection_events."""
    interruptions = sum(1 for e in events if e.get("event") == "disconnect_detected")
    reconnects = sum(1 for e in events if e.get("event") in
                     {"reconnected_in_thread", "rebuilt"})
    resubscribes = sum(1 for e in events if e.get("event") in
                       {"resubscribed", "stale_resubscribe"})
    auth_failures = sum(1 for e in events if e.get("event") == "auth_failed_terminal")
    return {
        "interruptions": interruptions,
        "reconnects": reconnects,
        "resubscribes": resubscribes,
        "auth_failures": auth_failures,
    }
