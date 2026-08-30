"""Controlled self-learning + champion/challenger promotion (Phase 14, 16, 17).

A training cycle: build a no-look-ahead dataset from accumulated candles, train
& walk-forward-evaluate a challenger, and promote it to champion ONLY if it
shows positive, statistically-significant out-of-sample expectancy AND beats the
incumbent. Otherwise it is saved (for the record) but not deployed. This is how
the app is allowed to "learn" without ever fabricating an edge.
"""

from __future__ import annotations

import math
import re
import time
from typing import Dict, Optional, Sequence

from services.backtester.engine import _norm_cdf
from services.learning_engine.dataset import build_dataset
from services.learning_engine.registry import ModelRecord, ModelRegistry
from services.learning_engine.train import train_and_select
from services.market_data.provider import CanonicalCandle

MIN_ROWS = 400               # refuse to "learn" from too little data
PROMOTE_P_VALUE = 0.01       # strict: held-out significance vs break-even
PROMOTE_MARGIN = 0.02        # must beat the champion by a real margin, not noise

# Version-bump thresholds (how much stronger the proven edge must be).
_MAJOR_DELTA = 0.10          # big expectancy gain -> "stronger model" (X+1.0.0)
_MAJOR_P = 0.001             # ...with strong significance
_MAJOR_EFF_N = 200           # ...on a large effective sample
_MINOR_DELTA = 0.03          # meaningful gain -> minor bump (x.Y+1.0)
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _bump_semver(current: str, level: str) -> str:
    m = _SEMVER.match(current or "") or _SEMVER.match("1.0.0")
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if level == "major":
        return f"{a + 1}.0.0"
    if level == "minor":
        return f"{a}.{b + 1}.0"
    return f"{a}.{b}.{c + 1}"


def _promotion_level(new_metrics: dict, champion: Optional[ModelRecord]) -> str:
    """How big is the proven improvement? major = strong change + strong tests;
    minor = meaningful; patch = small but real."""
    if champion is None:
        return "initial"
    delta = ((new_metrics.get("oos_expectancy", 0) or 0)
             - ((champion.metrics or {}).get("oos_expectancy", 0) or 0))
    p = new_metrics.get("p_value_one_sided", 1.0)
    eff = new_metrics.get("effective_n", 0) or 0
    if delta >= _MAJOR_DELTA and p is not None and p < _MAJOR_P and eff >= _MAJOR_EFF_N:
        return "major"
    if delta >= _MINOR_DELTA:
        return "minor"
    return "patch"


def _is_promotable(new_metrics: dict, champion: Optional[ModelRecord]) -> bool:
    """Deploy a challenger ONLY when its HELD-OUT gate metrics show a positive,
    strictly-significant edge that also beats the incumbent by a margin.

    This is conservative by design, but it is NOT immune to sequential testing:
    running many cycles across many assets still tests the null repeatedly, so a
    promoted model is a hypothesis to confirm with forward live performance
    (drift monitoring), never a proven edge.
    """
    if not new_metrics.get("held_out_gate"):
        return False  # never promote on non-held-out (winner's-curse) metrics
    exp = new_metrics.get("oos_expectancy", 0) or 0
    p = new_metrics.get("p_value_one_sided", 1.0)
    if exp <= 0 or p is None or p >= PROMOTE_P_VALUE:
        return False
    if champion is None:
        return True
    champ_exp = (champion.metrics or {}).get("oos_expectancy", -1) or -1
    return exp > champ_exp + PROMOTE_MARGIN


def run_training_cycle(
    candles: Sequence[CanonicalCandle],
    horizon_s: float,
    registry: ModelRegistry,
    payout: float = 0.8,
    now: Optional[float] = None,
) -> Dict[str, object]:
    now = now if now is not None else time.time()
    rows, names = build_dataset(candles, horizon_s)
    if len(rows) < MIN_ROWS:
        return {"status": "insufficient_data", "rows": len(rows), "need": MIN_ROWS}

    # Overlap factor: consecutive dataset rows are 1 candle apart but each label
    # spans `horizon_s`, so outcomes overlap when horizon > the candle step.
    closed = [c for c in candles if c.complete]
    step = closed[0].timeframe_s if closed and closed[0].timeframe_s else horizon_s
    overlap = max(1.0, horizon_s / step) if step else 1.0

    outcome = train_and_select(rows, names, payout=payout, overlap=overlap)
    if outcome is None:
        return {"status": "no_trainable_model", "rows": len(rows)}

    champion = registry.champion_record()
    promote = _is_promotable(outcome.metrics, champion)

    # Versioning: a PROMOTED model gets a semantic version reflecting how much
    # stronger the proven edge is (initial=1.0.0, patch=small, minor=meaningful,
    # major=strong change + strong tests). A challenger that doesn't clear the
    # gate is recorded as a "-rc" candidate but never becomes the version the
    # app serves — a new number only when it is genuinely better.
    level = _promotion_level(outcome.metrics, champion)
    if promote:
        prev = champion.version if (champion and _SEMVER.match(champion.version or "")) else "1.0.0"
        version = "1.0.0" if champion is None else _bump_semver(prev, level)
    else:
        version = f"{registry.new_version()}-rc"
    outcome.metrics["promotion_level"] = level if promote else "challenger"

    record = ModelRecord(
        version=version,
        feature_version=outcome.metrics.get("feature_version", "features-1.0.0"),
        model_kind=outcome.kind,
        created_at=now,
        train_period=[rows[0].timestamp, rows[-1].timestamp],
        n_train=outcome.n_train,
        n_test=outcome.n_test,
        metrics=outcome.metrics,
        notes=(f"promoted to champion ({level})" if promote
               else "kept as challenger (no significant edge over champion/break-even)"),
    )
    registry.save(outcome.model, record, make_champion=promote)
    return {
        "status": "trained",
        "version": version,
        "kind": outcome.kind,
        "promoted": promote,
        "promotion_level": level if promote else "challenger",
        "champion": registry.champion_version,
        "metrics": outcome.metrics,
    }


def detect_drift(recent_wins: int, recent_n: int, baseline_win_rate: float) -> Dict[str, object]:
    """Flag performance drift: is the recent live win rate significantly BELOW
    the model's expected (training/baseline) win rate? One-sided test."""
    if recent_n < 30 or not (0 < baseline_win_rate < 1):
        return {"status": "insufficient", "recent_n": recent_n}
    wr = recent_wins / recent_n
    se = math.sqrt(baseline_win_rate * (1 - baseline_win_rate) / recent_n)
    z = (wr - baseline_win_rate) / se if se > 0 else 0.0
    p_below = _norm_cdf(z)  # P(observing this low or lower under baseline)
    drifting = p_below < 0.05
    return {
        "status": "drift" if drifting else "ok",
        "recent_win_rate": round(wr, 4),
        "baseline_win_rate": round(baseline_win_rate, 4),
        "recent_n": recent_n,
        "p_below": round(p_below, 4),
        "action": "trigger research / evaluate challenger" if drifting else "none",
    }
