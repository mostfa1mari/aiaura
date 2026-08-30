"""Strategy modules + ensemble tests (offline)."""

from services.strategies import STRATEGIES, ensemble, strategy_signal_fn
from services.strategies.strategies import (
    breakout,
    mean_reversion,
    momentum,
    trend_following,
)


def test_trend_following_direction():
    assert trend_following({"trend_ema9_minus_ema21": 0.5, "trend_ema9_slope3": 0.2}).direction == 1
    assert trend_following({"trend_ema9_minus_ema21": -0.5}).direction == -1
    assert trend_following({}).direction == 0  # no features -> neutral, never guesses


def test_momentum_direction():
    assert momentum({"mom_rsi14_centered": 0.4}).direction == 1
    assert momentum({"mom_rsi14_centered": -0.4}).direction == -1


def test_mean_reversion_fades_extremes():
    assert mean_reversion({"mom_rsi14": 20}).direction == 1     # oversold -> BUY
    assert mean_reversion({"mom_rsi14": 85}).direction == -1    # overbought -> SELL
    assert mean_reversion({"mom_rsi14": 50}).direction == 0


def test_breakout():
    assert breakout({"pa_breakout_up": 1.0}).direction == 1
    assert breakout({"pa_breakout_down": 1.0}).direction == -1
    assert breakout({}).direction == 0


def test_ensemble_combines_and_reports_agreement():
    feats = {"trend_ema9_minus_ema21": 0.6, "trend_ema9_slope3": 0.3,
             "mom_rsi14_centered": 0.5, "mom_roc10": 0.01, "pa_body_over_range": 0.4}
    e = ensemble(feats)
    assert e.signal == "BUY"
    assert e.contributors >= 2
    assert 0 <= e.agreement <= 1
    assert len(e.per_strategy) == len(STRATEGIES)


def test_ensemble_never_returns_wait():
    e = ensemble({})  # no features at all
    assert e.signal in ("BUY", "SELL")


def test_strategy_signal_fn_shape():
    fn = strategy_signal_fn(trend_following)
    side, strength = fn({"trend_ema9_minus_ema21": 0.5}, [])
    assert side in ("BUY", "SELL") and 0 <= strength <= 1
