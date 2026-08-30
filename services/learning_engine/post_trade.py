"""Post-trade analysis (Phase 14).

When a signal loses, record WHY: which sub-signals pushed the wrong way, which
were actually right but out-voted, the regime, and the signal's conviction. This
turns each loss into structured, queryable evidence — and the aggregate feeds
the batched retraining (the model is never changed on a single trade).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Dict, List, Optional


def analyze_loss(prediction: Dict) -> Dict:
    """Explain one losing prediction from its stored context.

    A LOSS means the market went the OTHER way (or flat): if the signal was BUY,
    the correct direction was DOWN, and vice versa. Sub-signals that agreed with
    the (losing) signal were 'wrong'; those that disagreed were 'right'.
    """
    signal = prediction.get("signal")
    correct_side = -1 if signal == "BUY" else 1   # opposite of the losing call
    losing_side = 1 if signal == "BUY" else -1

    ctx = prediction.get("context_json")
    subs: List[dict] = []
    if ctx:
        try:
            subs = (json.loads(ctx) or {}).get("sub_signals", []) or []
        except (json.JSONDecodeError, TypeError):
            subs = []

    wrong = [s["name"] for s in subs if s.get("direction") == losing_side]
    right = [s["name"] for s in subs if s.get("direction") == correct_side]

    return {
        "signal": signal,
        "regime": prediction.get("regime"),
        "strength": prediction.get("strength"),
        "agreement": prediction.get("agreement"),
        "correct_direction": "UP" if correct_side == 1 else "DOWN",
        "misleading_signals": wrong,       # pushed toward the losing call
        "contrarian_correct": right,       # were right but out-voted
        "model_version": prediction.get("model_version"),
        "note": ("High-conviction loss — worth investigating this regime."
                 if (prediction.get("strength") or 0) > 0.6 else
                 "Low-conviction loss — expected noise at this strength."),
    }


def aggregate_losses(losses: List[Dict]) -> Dict:
    """Roll up recent losses: which strategies/regimes lose most. Advisory —
    a small sample is not proof (n is reported)."""
    misleading = Counter()
    by_regime = Counter()
    high_conv = 0
    for row in losses:
        aj = row.get("analysis_json")
        a = None
        if aj:
            try:
                a = json.loads(aj)
            except (json.JSONDecodeError, TypeError):
                a = None
        if a is None:
            a = analyze_loss(row)
        for name in a.get("misleading_signals", []):
            misleading[name] += 1
        if a.get("regime"):
            by_regime[a["regime"]] += 1
        if (a.get("strength") or 0) > 0.6:
            high_conv += 1
    return {
        "n_losses": len(losses),
        "most_misleading_strategies": misleading.most_common(5),
        "losses_by_regime": by_regime.most_common(5),
        "high_conviction_losses": high_conv,
        "note": ("Advisory only — a small number of losses is not proof. Feeds "
                 "the batched retraining, which changes the model only when a "
                 "held-out edge is confirmed."),
    }
