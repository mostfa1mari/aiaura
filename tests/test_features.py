"""Feature engine tests (offline). No-look-ahead is a first-class check."""

import math

from services.feature_engine import compute_features, feature_names
from services.feature_engine import indicators as ind
from services.market_data.provider import CanonicalCandle

TF = 60
BASE = 1787000000


def candle(i, o, h, l, c, complete=True):
    return CanonicalCandle(asset="EURUSD_otc", timeframe_s=TF, timestamp=float(BASE + i * TF),
                           open=o, high=h, low=l, close=c, tick_count=10,
                           complete=complete, provider="pocket_option")


def uptrend(n=80, start=1.10, step=0.001):
    out, p = [], start
    for i in range(n):
        o = p
        p = round(p + step, 6)
        out.append(candle(i, o, p + 0.0003, o - 0.0003, p))
    return out


def test_indicators_basic():
    vals = [1, 2, 3, 4, 5]
    assert ind.sma(vals, 5) == 3
    assert ind.sma(vals, 10) is None
    assert abs(ind.ema(vals, 3) - ind.ema_series(vals, 3)[-1]) < 1e-9
    assert ind.rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 14) == 100.0
    assert ind.roc([1, 1, 1, 2], 3) == 1.0


def test_feature_vector_on_uptrend():
    fv = compute_features(uptrend())
    v = fv.values
    assert fv.asset == "EURUSD_otc" and fv.timeframe_s == TF
    # clear uptrend signatures
    assert v["trend_ema9_above_ema21"] == 1.0
    assert v["mom_rsi14"] > 55
    assert v["pa_bull"] == 1.0
    assert v.get("adx_bull") == 1.0
    assert v["pa_consecutive"] > 0


def test_feature_vector_on_downtrend():
    down = [candle(i, o, o + 0.0003, c - 0.0003, c) for i, (o, c) in
            enumerate((round(1.30 - i * 0.001, 6), round(1.30 - (i + 1) * 0.001, 6)) for i in range(80))]
    v = compute_features(down).values
    assert v["trend_ema9_above_ema21"] == 0.0
    assert v["mom_rsi14"] < 45
    assert v["pa_consecutive"] < 0


def test_no_lookahead_swing_confirmation():
    # swing_points is the only indicator with a right-window (future-facing);
    # this is the real leakage vector. Property: confirming a swing from a
    # PREFIX must give exactly the full-series swings whose right-window fits in
    # the prefix — future bars can never add/change an earlier swing.
    import random
    random.seed(5)
    highs = [round(1.0 + random.random(), 3) for _ in range(60)]
    lows = [round(h - 0.1 - random.random() * 0.05, 4) for h in highs]
    left = right = 2
    full_sh, full_sl = ind.swing_points(highs, lows, left, right)
    checked = 0
    for k in range(left + right, len(highs)):
        sh_k, sl_k = ind.swing_points(highs[: k + 1], lows[: k + 1], left, right)
        assert sh_k == [i for i in full_sh if i <= k - right]
        assert sl_k == [i for i in full_sl if i <= k - right]
        checked += 1
    assert checked > 10  # actually exercised the invariant


def test_features_are_as_of_last_candle():
    full = uptrend(80)
    fv = compute_features(full[:50])
    assert fv.at_timestamp == full[49].timestamp


def test_forming_candle_excluded():
    candles = uptrend(40) + [candle(41, 1.20, 1.25, 1.05, 1.06, complete=False)]
    fv = compute_features(candles)
    # last COMPLETE candle is index 39; forming one must not move at_timestamp
    assert fv.at_timestamp == candles[39].timestamp


def test_insufficient_data_is_safe():
    fv = compute_features([candle(0, 1.1, 1.1, 1.1, 1.1)])
    assert fv.values == {}  # < 2 candles -> empty, no crash


def test_all_features_finite_and_named():
    fv = compute_features(uptrend())
    for name, val in fv.values.items():
        assert math.isfinite(val), name
        assert name in feature_names(), f"{name} missing from feature_names()"


def test_as_row_dense_and_ordered():
    fv = compute_features(uptrend())
    names = feature_names()
    row = fv.as_row(names)
    assert len(row) == len(names)
    assert all(math.isfinite(x) for x in row)
