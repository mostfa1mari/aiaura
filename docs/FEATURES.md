# Feature Engine (Phase 7)

Code: `services/feature_engine/`. `compute_features(candles)` returns a named,
versioned `FeatureVector` computed **as of the last closed candle**, using only
`candles[:i+1]` — no look-ahead. Forming candles are excluded.

## Groups (36 features, `FEATURE_VERSION = features-1.0.0`)

| Group | Examples |
|---|---|
| Trend | EMA9/21 vs close, EMA9−EMA21, EMA slope, close−SMA50, persistence |
| Momentum | RSI(14), MACD line/hist, Stochastic %K, ROC(10) |
| Volatility | ATR(14), realized vol, Bollinger width, expansion ratio |
| Trend strength | ADX(14), +DI/−DI, DI direction |
| Price action | body/range, upper/lower wick ratios, consecutive candles, breakouts |
| Structure | distance to last swing high/low, nearest support/resistance, range |

Price-scaled features are normalized by recent volatility so they are
comparable across assets/price levels. Missing features (insufficient history)
are omitted from the dict; `feature_names()` gives a stable order and
`FeatureVector.as_row(names)` builds a dense ML row (0.0 fill).

## No look-ahead

The only future-facing indicator is `swing_points`, which uses a right-window
to confirm a swing. It only returns swings whose right-window fits **within the
input**, and the input ends at the current candle — so a swing at index *i* is
confirmed solely by candles ≤ *i+right ≤ current*. `tests/test_features.py`
proves this: `swing_points(prefix)` equals the full-series swings filtered to
indices confirmable within the prefix (a real leakage check, not a tautology).
