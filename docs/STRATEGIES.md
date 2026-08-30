# Strategies, Similarity, Latency (Phases 8, 13, 20)

## Strategy modules (Phase 8)

Code: `services/strategies/`. Nine independent, inspectable strategies over the
no-look-ahead feature dict, each returning a direction (+1/0/−1) and score:
trend-following, momentum, mean-reversion, breakout, reversal,
support/resistance, price-action, volatility-breakout, adx-trend. A strategy
returns **neutral** when the features it needs are absent — it never guesses.

- `ensemble(features)` averages the non-neutral scores into one BUY/SELL and an
  agreement measure (fraction of active strategies on the final side).
- `strategy_signal_fn(strategy)` adapts any single strategy to the backtester,
  so each can be evaluated independently with honest metrics
  (docs/BACKTESTING.md) before being trusted.

The app surfaces the ensemble's agreement and contributor count as secondary
info on each signal (transparency, not a second claim of edge).

## Historical similarity (Phase 13)

Code: `services/similarity/`. `HistoricalSimilarity` is a k-NN over standardized
feature vectors of past states. `query(features, k, as_of)` returns the nearest
prior states and their forward directional rate ("in similar past states, price
went UP X% of the time"). `as_of` restricts to states strictly before the
decision time — no look-ahead. It reports `n_neighbors` and a `confident` flag,
and never treats a tiny sample as proof. The app shows this per signal; it may
legitimately **disagree** with the signal, which is surfaced honestly.

## Latency viability (Phase 20)

Code: `services/latency/`. Short expiries can be eaten by latency. `assess(...)`
sums the stale-quote age + prediction latency + assumed user execution time and
compares it to the horizon: **viable** (<15%), **marginal** (<40%),
**not_viable** (≥40%). `summarize(...)` judges a batch on the p95 (worst
realistic) fraction. The app shows the per-signal verdict, so a 3s/5s expiry is
never presented as reliably actionable without the numbers backing it.
