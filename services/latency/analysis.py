"""Latency → viability assessment for short horizons."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence

# Default assumption for how long the user needs to read the signal and click
# on Pocket Option. Configurable; be conservative for short expiries.
DEFAULT_EXECUTION_MS = 1500.0


@dataclass
class LatencyVerdict:
    horizon_s: float
    total_latency_ms: float
    fraction_of_horizon: float
    verdict: str            # viable | marginal | not_viable
    detail: str = ""


def assess(prediction_latency_ms: float, horizon_s: float,
           tick_age_ms: float = 0.0, execution_ms: float = DEFAULT_EXECUTION_MS) -> LatencyVerdict:
    """One assessment. total latency = stale-quote age + prediction + execution.

    verdict: viable (<15% of horizon), marginal (<40%), not_viable (>=40%).
    """
    total = max(0.0, tick_age_ms) + max(0.0, prediction_latency_ms) + max(0.0, execution_ms)
    horizon_ms = horizon_s * 1000.0
    frac = total / horizon_ms if horizon_ms > 0 else float("inf")
    if frac < 0.15:
        verdict = "viable"
    elif frac < 0.40:
        verdict = "marginal"
    else:
        verdict = "not_viable"
    return LatencyVerdict(
        horizon_s=horizon_s, total_latency_ms=round(total, 1),
        fraction_of_horizon=round(frac, 3), verdict=verdict,
        detail=(f"{total:.0f}ms of a {horizon_ms:.0f}ms horizon "
                f"(quote {tick_age_ms:.0f} + predict {prediction_latency_ms:.0f} + exec {execution_ms:.0f})"),
    )


def summarize(prediction_latencies_ms: Sequence[float], horizon_s: float,
              tick_ages_ms: Optional[Sequence[float]] = None,
              execution_ms: float = DEFAULT_EXECUTION_MS) -> dict:
    """Aggregate viability over many measured predictions."""
    if not prediction_latencies_ms:
        return {"horizon_s": horizon_s, "n": 0, "verdict": "unknown", "note": "no samples"}
    ticks = list(tick_ages_ms) if tick_ages_ms else [0.0] * len(prediction_latencies_ms)
    verdicts = [assess(p, horizon_s, t, execution_ms)
                for p, t in zip(prediction_latencies_ms, ticks)]
    fracs = [v.fraction_of_horizon for v in verdicts]
    p95 = sorted(fracs)[min(len(fracs) - 1, int(0.95 * len(fracs)))]
    # judge on the p95 (worst realistic case), not the mean
    worst = "viable" if p95 < 0.15 else ("marginal" if p95 < 0.40 else "not_viable")
    return {
        "horizon_s": horizon_s,
        "n": len(verdicts),
        "mean_fraction": round(statistics.mean(fracs), 3),
        "p95_fraction": round(p95, 3),
        "verdict": worst,
        "execution_ms_assumed": execution_ms,
        "note": ("Short-horizon viability judged on the p95 latency fraction. "
                 "'marginal'/'not_viable' means latency eats too much of the "
                 "expiry for a signal to be reliably actionable."),
    }
