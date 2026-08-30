"""Train + honestly evaluate ML models (Phase 10) with walk-forward CV.

Statistical honesty is the whole point here, so the design is deliberately
conservative:

* **Model selection and the gating significance test use DISJOINT data.** The
  model kind is chosen on walk-forward folds over an early *selection* region;
  the deploy/promote decision is judged ONCE on a later *held-out gate* region
  the selection never touched. This removes the winner's-curse inflation that
  arises from selecting the best of several models on the same test set.
* **Overlapping trades are not independent.** With a horizon spanning k candles,
  consecutive trades share their outcome window, so the effective sample size is
  ~n/k. All confidence/p-value math uses that effective N, not the raw count —
  otherwise significance is overstated by ~sqrt(k).

Even so, repeated training cycles are sequential tests; a single passing cycle
is a hypothesis to confirm with forward live performance (drift monitoring), not
a proven edge. See self_learning for the promotion gate and its caveats.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from services.backtester.engine import _norm_cdf, _wilson
from services.learning_engine.dataset import DatasetRow

MIN_GATE = 60           # min held-out gate rows
MIN_EFF_N = 25          # min effective (de-overlapped) sample on the gate


@dataclass
class TrainOutcome:
    model: object
    kind: str
    metrics: Dict[str, object]
    n_train: int
    n_test: int


def _candidates():
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "logistic": lambda: make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced"),
        ),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=200, max_depth=6, min_samples_leaf=20,
            class_weight="balanced", random_state=0, n_jobs=-1,
        ),
        "gradient_boost": lambda: GradientBoostingClassifier(
            n_estimators=150, max_depth=3, learning_rate=0.05, random_state=0,
        ),
    }


def _score(preds: Sequence, actual: Sequence, payout: float, overlap: float) -> Optional[dict]:
    """Metrics for a set of directional predictions, honest about overlap.

    ``overlap`` = horizon / candle-step; consecutive trades share their outcome
    window, so the effective independent sample size is trades / overlap.
    """
    trades = len(preds)
    if trades == 0:
        return None
    wins = sum(1 for p, a in zip(preds, actual) if int(p) == int(a))
    wr = wins / trades
    n_eff = max(1.0, trades / max(1.0, overlap))
    be = 1.0 / (1.0 + payout) if payout > 0 else 0.5
    se = math.sqrt(be * (1 - be) / n_eff)
    z = (wr - be) / se if se > 0 else 0.0
    # Wilson interval on the EFFECTIVE sample (wider, honest).
    eff_wins = wr * n_eff
    lo, hi = _wilson(eff_wins, n_eff)
    return {
        "oos_trades": trades,
        "effective_n": round(n_eff, 1),
        "oos_win_rate": round(wr, 4),
        "oos_wilson_ci": [round(lo, 4), round(hi, 4)],
        "breakeven_win_rate": round(be, 4),
        "oos_expectancy": round(wr * payout - (1 - wr), 4),
        "p_value_one_sided": round(1.0 - _norm_cdf(z), 4),
        "overlap": round(overlap, 2),
    }


def _mean_expectancy_walk_forward(make, X, y, payout, overlap, folds, min_train) -> Optional[float]:
    from services.backtester.engine import walk_forward_splits

    splits = walk_forward_splits(len(X), folds=folds, min_train=min_train)
    if not splits:
        return None
    exps: List[float] = []
    for (tr0, tr1), (te0, te1) in splits:
        Xtr, ytr = X[tr0:tr1], y[tr0:tr1]
        Xte, yte = X[te0:te1], y[te0:te1]
        if len(set(ytr)) < 2 or not Xte:
            continue
        model = make()
        model.fit(Xtr, ytr)
        m = _score(model.predict(Xte), yte, payout, overlap)
        if m:
            exps.append(m["oos_expectancy"])
    return (sum(exps) / len(exps)) if exps else None


def train_and_select(
    rows: Sequence[DatasetRow],
    feature_names: Sequence[str],
    payout: float = 0.8,
    overlap: float = 1.0,
    folds: int = 4,
    min_train: int = 100,
    gate_frac: float = 0.3,
) -> Optional[TrainOutcome]:
    rows = sorted(rows, key=lambda r: r.timestamp)
    X = [r.features for r in rows]
    y = [r.label for r in rows]
    n = len(rows)

    gate_size = max(MIN_GATE, int(n * gate_frac))
    sel_end = n - gate_size
    if sel_end < min_train + folds or gate_size < MIN_GATE:
        return None
    Xsel, ysel = X[:sel_end], y[:sel_end]
    Xgate, ygate = X[sel_end:], y[sel_end:]
    if len(set(ysel)) < 2 or len(set(ygate)) < 2:
        return None

    # 1) SELECT the model kind on walk-forward folds within the selection region.
    scored: List[Tuple[str, float]] = []
    for kind, make in _candidates().items():
        me = _mean_expectancy_walk_forward(make, Xsel, ysel, payout, overlap, folds, min_train)
        if me is not None:
            scored.append((kind, me))
    if not scored:
        return None
    scored.sort(key=lambda kv: kv[1], reverse=True)
    best_kind = scored[0][0]

    # 2) GATE: train the chosen kind on all selection data, judge ONCE on the
    #    untouched held-out gate region (unbiased significance).
    gate_model = _candidates()[best_kind]()
    gate_model.fit(Xsel, ysel)
    gate_metrics = _score(gate_model.predict(Xgate), ygate, payout, overlap)
    if gate_metrics is None or gate_metrics["effective_n"] < MIN_EFF_N:
        return None

    # 3) DEPLOY: refit the chosen kind on ALL data (evaluation already done).
    final = _candidates()[best_kind]()
    final.fit(X, y)

    metrics = dict(gate_metrics)
    metrics.update({
        "selected_kind": best_kind,
        "n_candidates": len(scored),
        "selection_scores": {k: round(m, 4) for k, m in scored},
        "held_out_gate": True,
        "feature_version": "features-1.0.0",
    })
    return TrainOutcome(model=final, kind=best_kind, metrics=metrics,
                        n_train=sel_end, n_test=gate_size)
