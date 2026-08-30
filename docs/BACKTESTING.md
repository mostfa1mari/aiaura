# Backtesting (Phase 9)

Code: `services/backtester/`. Event-driven replay of closed candles with strict
no-look-ahead and **honest** metrics.

## How it works

For each candle after warmup, `backtest(candles, signal_fn, config)`:
1. Computes features/signal from `candles[:i+1]` only (the signal never sees a
   future candle — `tests/test_backtester.py` checks the per-entry bound).
2. Reads the entry price (quote in effect at the candle) and the expiry price
   `horizon_s` later (last close at or before, requiring the series to cover
   it — no extrapolation).
3. Scores the outcome UP/DOWN/FLAT; a BUY wins on UP, a SELL on DOWN; FLAT is a
   loss by default (configurable).

## Metrics — and why they are conservative

- **Effective sample size.** Trades are entered every candle but each spans
  `horizon_s`; when the horizon covers `k` candles, consecutive trades overlap
  and are **not independent**. The Wilson confidence interval and the
  break-even significance test use `effective_n = trades / overlap`, not the raw
  count. Ignoring this would make intervals ~`√k` too narrow and manufacture
  significance.
- **Payout-adjusted expectancy** = `win_rate·payout − (1−win_rate)` (stake
  units); **break-even win rate** = `1/(1+payout)`.
- Wilson CI, one-sided p-value vs break-even (on `effective_n`), profit factor,
  max losing streak, and breakdowns by hour and by signal side.
- `has_edge_evidence` is True only when `p < 0.05` **and** expectancy > 0
  **and** `effective_n ≥ 100`. It is *evidence*, to be confirmed by forward
  live performance — never a guarantee.

## Walk-forward

`walk_forward_splits(n, folds)` produces chronological expanding-window splits
with each test window strictly after its train window — no shuffling, no
leakage. Used by the ML trainer's selection stage.
