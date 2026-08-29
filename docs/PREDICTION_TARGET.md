# Prediction Target (Phase 6)

Defines exactly **what AI AURA predicts** and **how a historical outcome is
measured**. Code: `services/labeling/target.py`; tests: `tests/test_target.py`.

## What is predicted

Directional movement over a chosen expiry — the binary outcome a Pocket Option
option settles on. For entry time `T` and horizon `H`:

```
effective_entry = T + entry_delay_s          # models execution latency
expiry          = effective_entry + H
entry_price     = reference_price(effective_entry)
expiry_price    = reference_price(expiry)
outcome (Direction):
    UP    if expiry_price >  entry_price      # a BUY wins
    DOWN  if expiry_price <  entry_price      # a SELL wins
    FLAT  if equal at the configured precision
```

The model predicts the outcome direction; a **BUY** signal is correct when the
outcome is **UP**, a **SELL** when **DOWN**.

## Reference-price methodology

`reference_price(t)` = the price of the **last tick at or before `t`** — the
quote in effect at that instant. This mirrors how a binary option reads the
quote at entry and at expiry. It is defined precisely so labels are
reproducible:

- **at or before**: a tick exactly at `t` is used; otherwise the most recent
  earlier tick.
- **never a future tick**: `entry_price` uses only ticks ≤ `effective_entry`;
  `expiry_price` uses only ticks ≤ `expiry`.
- **tied timestamps are deterministic**: when several ticks share a
  `source_timestamp`, the last-arriving one (by `received_timestamp`, then
  `tick_id`) is "the quote in effect" — chosen the same way regardless of input
  order, so labels are reproducible even when ticks come from unordered storage
  queries or merged sources.

`ReferenceSeries` provides O(log n) lookup over a sorted tick series.

## No look-ahead

- The **features** (Phase 7) may use only data at or before `T`.
- The **label** references the future `expiry` price and is therefore a
  training/backtest TARGET produced offline. It must never be an input to the
  feature engine or the live signal path. (`services/labeling` is imported by
  the backtester and trainer, never by the feature/signal engine.)
- Test `test_no_lookahead_entry_price_ignores_future_moves` pins the guarantee:
  a price jump after the effective entry does not leak into `entry_price`.

## Accounting for reality (no fabrication)

| Concern | Handling |
|---|---|
| Execution latency | `entry_delay_s` shifts the effective entry; 0 = pure model eval, set >0 to model real latency (see Phase 20). |
| Price precision / ties | `price_precision` rounds both quotes before compare; equal → `FLAT`. `FLAT` is reported honestly (common at short expiries) — on most binary platforms it is a loss, but tie handling is left to the evaluator. |
| Missing ticks near a boundary | If the nearest prior tick is older than `max_reference_staleness_s`, the reference is invalid. |
| Data does not bracket a point | Entry before the first tick, or expiry after the last tick (the moment was never observed) → invalid. No extrapolation. |
| Invalid labels | `valid=False` with a `reason`; these MUST be excluded from training/backtests, never guessed. |

## Config

`LabelConfig(horizon_s, entry_delay_s=0, max_reference_staleness_s=2.0,
price_precision=None)`. `infer_price_precision(ticks)` estimates a sensible
`price_precision` from observed quotes (e.g. 5 for EURUSD_otc).

## Usage

```python
from services.labeling import ReferenceSeries, LabelConfig, generate_labels

series = ReferenceSeries(ticks)                       # built once
cfg = LabelConfig(horizon_s=5, price_precision=5)     # 5s expiry
labels = generate_labels(series, entry_times, cfg)    # one Label per entry
valid = [l for l in labels if l.valid]                # drop insufficient-data
win_rate_if_always_buy = sum(l.buy_wins for l in valid) / len(valid)
```

Horizons align with the candle engine timeframes (docs/CANDLE_ENGINE.md):
3s, 5s, 10s, 15s, 30s, 1m, 3m, 5m, 15m.
