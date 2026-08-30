# Machine Learning & Self-Learning (Phases 10, 11, 14, 15, 16, 17)

Code: `services/learning_engine/` + `services/signal_engine/ml_predictor.py`.
The guiding rule is **honesty about evidence**: the machinery must never claim
or deploy an edge that the data does not support.

## Pipeline

```
candles → build_dataset (no look-ahead)   services/learning_engine/dataset.py
        → train_and_select (walk-forward)  services/learning_engine/train.py
        → ModelRegistry (champion/challenger)  registry.py
        → self_learning gate (promote?)    self_learning.py
        → MLPredictor (BUY/SELL)            services/signal_engine/ml_predictor.py
```

The app (`apps/api`) uses the champion model if one is deployed, otherwise the
transparent baseline. Until a model is *promoted*, there is no champion and the
baseline is used.

## Dataset (no look-ahead)

`build_dataset(candles, horizon_s)` emits one row per closed candle after
warmup: features computed from `candles[:i+1]` (past only) and the binary
outcome (UP=1 / DOWN=0) over `horizon_s` using future closes (the target).
FLAT outcomes are excluded. The label is never an input to the features.

## Training & evaluation — the honesty design

Two statistical traps are handled explicitly (both were caught by adversarial
review and fixed):

1. **Overlapping trades are not independent.** With a horizon spanning `k`
   candles, consecutive trades share their outcome window, so the effective
   sample size is `≈ n/k`. All confidence intervals and p-values use this
   **effective N**, not the raw count — otherwise significance is overstated by
   `≈√k`. (`overlap` is reported in every metrics block.)
2. **Winner's-curse.** Selecting the best of several models on the same test set
   inflates its apparent edge. So selection and the gating test use **disjoint
   data**: the model kind is chosen by walk-forward folds on an early
   *selection* region; the deploy decision is judged **once** on a later
   *held-out gate* region the selection never touched. Metrics carry
   `held_out_gate: true`.

Candidates: logistic regression (scaled), random forest, gradient boosting.
Scoring is chronological (never shuffled) and uses directional win rate +
payout-adjusted expectancy — the way the product is actually used — not
in-sample accuracy.

## Promotion gate (champion/challenger — Phase 16)

`_is_promotable` deploys a challenger **only** if, on the held-out gate:
`held_out_gate` is true, expectancy > 0, `p_value_one_sided < 0.01` (strict),
and it beats the incumbent's expectancy by a margin (≥ 0.02). Nothing is ever
auto-promoted for being first. Rollback = promote an older version; nothing is
deleted.

## Residual risk — stated plainly (Phase 15)

Even with all of the above, **repeated training cycles are sequential tests**:
run enough cycles across enough assets and the null will eventually be rejected
by chance. So a promoted champion is a **hypothesis to confirm with forward live
performance**, not a proven edge. `detect_drift` (Phase 17) compares recent live
win rate to the model's expected rate and flags degradation on the research
dashboard. Treat a fresh champion as on-probation until forward results hold up.

## Running it

```
.venv/Scripts/python scripts/train.py --asset EURUSD_otc --expiry 60 --pages 8
```

On data with no real edge this prints "challenger kept but NOT deployed" — the
honest outcome. More history (higher `--pages`, or days of collected ticks) →
more reliable training. The champion (if any) is served automatically on the
next API start.
