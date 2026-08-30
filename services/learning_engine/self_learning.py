"""Controlled self-learning + champion/challenger promotion (Phase 14, 16, 17).

A training cycle: build a no-look-ahead dataset from accumulated candles, train
& walk-forward-evaluate a challenger, and promote it to champion ONLY if it
shows positive, statistically-significant out-of-sample expectancy AND beats the
incumbent. Otherwise it is saved (for the record) but not deployed. This is how
the app is allowed to "learn" without ever fabricating an edge.
"""

from __future__ import annotations

import math
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

    version = registry.new_version()
    record = ModelRecord(
        version=version,
        feature_version=outcome.metrics.get("feature_version", "features-1.0.0"),
        model_kind=outcome.kind,
        created_at=now,
        train_period=[rows[0].timestamp, rows[-1].timestamp],
        n_train=outcome.n_train,
        n_test=outcome.n_test,
        metrics=outcome.metrics,
        notes="promoted to champion" if promote else "kept as challenger (not deployed)",
    )
    registry.save(outcome.model, record, make_champion=promote)
    return {
        "status": "trained",
        "version": version,
        "kind": outcome.kind,
        "promoted": promote,
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
