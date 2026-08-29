"""Baseline signal engine + store tests (offline)."""

from services.market_data.provider import CanonicalCandle
from services.signal_engine import generate_signal
from services.signal_engine.store import SignalStore

TF = 5
BASE = 1787000000


def candle(i: int, o: float, h: float, l: float, c: float, complete: bool = True) -> CanonicalCandle:
    return CanonicalCandle(
        asset="EURUSD_otc", timeframe_s=TF, timestamp=float(BASE + i * TF),
        open=o, high=h, low=l, close=c, tick_count=10, complete=complete,
        provider="pocket_option",
    )


def _uptrend(n=60):
    out = []
    p = 1.10
    for i in range(n):
        o = p
        p = p + 0.001
        out.append(candle(i, o, p + 0.0005, o - 0.0005, p))
    return out


def _downtrend(n=60):
    out = []
    p = 1.30
    for i in range(n):
        o = p
        p = p - 0.001
        out.append(candle(i, o, o + 0.0005, p - 0.0005, p))
    return out


def test_uptrend_gives_buy():
    r = generate_signal(_uptrend(), TF)
    assert r.signal == "BUY"
    assert r.score > 0
    assert r.data_sufficiency == 1.0
    assert r.agreement > 0.5


def test_downtrend_gives_sell():
    r = generate_signal(_downtrend(), TF)
    assert r.signal == "SELL"
    assert r.score < 0


def test_always_buy_or_sell_never_wait():
    # flat / minimal data must still yield a side (UI never shows WAIT)
    flat = [candle(i, 1.10, 1.10, 1.10, 1.10) for i in range(5)]
    r = generate_signal(flat, TF)
    assert r.signal in ("BUY", "SELL")


def test_empty_candles_still_returns_side():
    r = generate_signal([], TF)
    assert r.signal in ("BUY", "SELL")
    assert r.data_sufficiency == 0.0
    assert r.candles_used == 0


def test_forming_candles_excluded():
    candles = _uptrend(40) + [candle(41, 1.20, 1.25, 1.19, 1.05, complete=False)]
    r = generate_signal(candles, TF)
    # the forming bearish candle must not flip a clear uptrend
    assert r.candles_used == 40
    assert r.signal == "BUY"


def test_store_roundtrip_and_stats(tmp_path):
    store = SignalStore(tmp_path / "t.db")
    sid = store.record_prediction(
        asset="EURUSD_otc", expiry_s=5, signal="BUY", score=0.4, strength=0.4,
        agreement=0.8, regime="trend_up", data_sufficiency=1.0, entry_price=1.1,
        market_ts=BASE, prediction_latency_ms=50.0, model_version="baseline-1.0.0",
        context={"x": 1},
    )
    assert store.get_prediction(sid)["result"] is None
    assert store.record_result(sid, "WIN") is True
    assert store.record_result(sid, "LOSS") is False  # already settled
    stats = store.stats()
    assert stats["wins"] == 1 and stats["settled"] == 1 and stats["win_rate"] == 1.0


def test_store_losing_streak(tmp_path):
    store = SignalStore(tmp_path / "s.db")
    import time
    for i, res in enumerate(["LOSS", "LOSS", "WIN", "LOSS", "LOSS", "LOSS"]):
        sid = store.record_prediction(
            asset="EURUSD_otc", expiry_s=5, signal="BUY", score=0.1, strength=0.1,
            agreement=0.5, regime="range", data_sufficiency=1.0, entry_price=1.1,
            market_ts=BASE + i, prediction_latency_ms=10.0, model_version="v",
            context={},
        )
        store.record_result(sid, res)
    assert store.stats()["max_losing_streak"] == 3


def test_no_probability_or_guarantee_claim():
    # the engine must not invent a win-probability field
    r = generate_signal(_uptrend(), TF)
    assert not hasattr(r, "win_probability")
    assert not hasattr(r, "accuracy")
