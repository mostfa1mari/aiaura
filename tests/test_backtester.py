"""Backtester tests (offline): correctness, no-look-ahead, honest metrics."""

import math

from services.backtester import BacktestConfig, backtest, walk_forward_splits
from services.market_data.provider import CanonicalCandle

TF = 60
BASE = 1787000000


def candle(i, c, o=None):
    o = c if o is None else o
    return CanonicalCandle(asset="EURUSD_otc", timeframe_s=TF, timestamp=float(BASE + i * TF),
                           open=o, high=max(o, c) + 1e-4, low=min(o, c) - 1e-4, close=c,
                           tick_count=10, complete=True, provider="pocket_option")


def monotonic_up(n=200, start=1.10, step=0.001):
    out, p = [], start
    for i in range(n):
        p = round(p + step, 6)
        out.append(candle(i, p))
    return out


def test_always_buy_wins_on_monotonic_uptrend():
    candles = monotonic_up(200)
    cfg = BacktestConfig(horizon_s=TF, payout=0.8, warmup=50)
    res = backtest(candles, lambda f, c: ("BUY", 1.0), cfg)
    assert res.trades > 100
    assert res.win_rate == 1.0            # every 1-candle-ahead move is up
    assert res.wins == res.trades and res.losses == 0
    assert res.expectancy > 0
    assert res.max_losing_streak == 0
    assert res.has_edge_evidence is True  # significant + positive + n>=100


def test_always_sell_loses_on_monotonic_uptrend():
    candles = monotonic_up(200)
    res = backtest(candles, lambda f, c: ("SELL", 1.0), BacktestConfig(horizon_s=TF, warmup=50))
    assert res.win_rate == 0.0
    assert res.expectancy < 0
    assert res.has_edge_evidence is False


def test_coinflip_has_no_edge_evidence():
    # deterministic pseudo-random-ish price walk that isn't predictable by "BUY"
    import math as _m
    out, p = [], 1.10
    for i in range(400):
        p = round(p + (0.001 if _m.sin(i * 1.3) > 0 else -0.001), 6)
        out.append(candle(i, p))
    res = backtest(out, lambda f, c: ("BUY", 1.0), BacktestConfig(horizon_s=TF, warmup=50))
    # around break-even; must NOT falsely claim an edge
    assert res.has_edge_evidence is False


def test_no_lookahead_signal_sees_exactly_up_to_entry():
    # Real per-entry check: at decision i the signal must see exactly
    # candles[:i+1] (last candle == the entry), never a future candle. A leaky
    # engine that passed the whole series would make every last-ts equal the
    # final candle, which this assertion would catch.
    seen_last_ts = []

    def sig(features, candles_so_far):
        seen_last_ts.append(candles_so_far[-1].timestamp)
        return ("BUY", 1.0)

    candles = monotonic_up(120)
    backtest(candles, sig, BacktestConfig(horizon_s=TF, warmup=50))
    expected = [candles[i].timestamp for i in range(50, 120)]
    assert seen_last_ts == expected


def test_effective_n_shrinks_with_overlap_and_widens_ci():
    candles = monotonic_up(300)
    # horizon spans 5 candles -> overlapping trades -> effective_n ~ trades/5
    res = backtest(candles, lambda f, c: ("BUY", 1.0),
                   BacktestConfig(horizon_s=5 * TF, warmup=50))
    assert res.overlap == 5.0
    assert res.effective_n is not None and res.effective_n < res.trades
    assert abs(res.effective_n - res.trades / 5.0) < 1.0
    # honest CI uses effective_n; a 1-candle horizon on the same data is tighter
    res1 = backtest(candles, lambda f, c: ("BUY", 1.0), BacktestConfig(horizon_s=TF, warmup=50))
    width5 = res.wilson_high - res.wilson_low
    width1 = res1.wilson_high - res1.wilson_low
    assert width5 > width1  # overlap widens the interval


def test_wilson_ci_and_breakeven():
    candles = monotonic_up(200)
    res = backtest(candles, lambda f, c: ("BUY", 1.0), BacktestConfig(horizon_s=TF, payout=0.8, warmup=50))
    assert res.breakeven_win_rate is not None
    assert abs(res.breakeven_win_rate - (1 / 1.8)) < 1e-9
    assert res.wilson_low <= (res.win_rate or 0) <= res.wilson_high + 1e-9


def test_min_strength_filter():
    candles = monotonic_up(120)
    res = backtest(candles, lambda f, c: ("BUY", 0.1), BacktestConfig(horizon_s=TF, warmup=50, min_strength=0.5))
    assert res.trades == 0 and res.skipped_weak > 0


def test_walk_forward_splits_are_chronological():
    splits = walk_forward_splits(600, folds=5, min_train=100)
    assert len(splits) == 5
    prev_test_end = 0
    for (tr0, tr1), (te0, te1) in splits:
        assert tr0 == 0 and tr1 == te0          # train ends where test begins
        assert te0 < te1 and te0 >= tr1          # test strictly after train
        assert te0 >= prev_test_end
        prev_test_end = te1


def test_flat_handling():
    flat = [candle(i, 1.10) for i in range(120)]  # no movement -> all FLAT
    res_loss = backtest(flat, lambda f, c: ("BUY", 1.0), BacktestConfig(horizon_s=TF, warmup=50, flat_is_loss=True))
    assert res_loss.trades > 0 and res_loss.wins == 0
    res_skip = backtest(flat, lambda f, c: ("BUY", 1.0), BacktestConfig(horizon_s=TF, warmup=50, flat_is_loss=False))
    assert res_skip.trades == 0  # ties excluded entirely
