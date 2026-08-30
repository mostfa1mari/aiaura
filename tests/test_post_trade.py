"""Post-trade loss analysis + UI-only reset (data-preserving) tests."""

import json
import time

from services.learning_engine.post_trade import aggregate_losses, analyze_loss
from services.signal_engine.store import SqliteSignalStore


def _pred(signal="BUY", subs=None):
    return {
        "signal": signal, "regime": "trend_up", "strength": 0.7, "agreement": 0.8,
        "model_version": "baseline-1.0.0",
        "context_json": json.dumps({"sub_signals": subs or []}),
    }


def test_analyze_loss_identifies_misleading_signals():
    # BUY that lost -> correct direction was DOWN; the +1 subs were misleading,
    # the -1 subs were contrarian-correct.
    subs = [{"name": "trend_following", "direction": 1},
            {"name": "momentum", "direction": 1},
            {"name": "mean_reversion", "direction": -1}]
    a = analyze_loss(_pred("BUY", subs))
    assert a["correct_direction"] == "DOWN"
    assert set(a["misleading_signals"]) == {"trend_following", "momentum"}
    assert a["contrarian_correct"] == ["mean_reversion"]
    assert "High-conviction" in a["note"]  # strength 0.7 > 0.6


def test_analyze_loss_sell():
    subs = [{"name": "breakout", "direction": -1}, {"name": "reversal", "direction": 1}]
    a = analyze_loss(_pred("SELL", subs))
    assert a["correct_direction"] == "UP"
    assert a["misleading_signals"] == ["breakout"]
    assert a["contrarian_correct"] == ["reversal"]


def test_aggregate_losses():
    losses = [
        {"analysis_json": json.dumps({"misleading_signals": ["trend_following"],
                                      "regime": "range", "strength": 0.7})},
        {"analysis_json": json.dumps({"misleading_signals": ["trend_following", "momentum"],
                                      "regime": "range", "strength": 0.3})},
    ]
    agg = aggregate_losses(losses)
    assert agg["n_losses"] == 2
    assert agg["most_misleading_strategies"][0] == ("trend_following", 2)
    assert agg["high_conviction_losses"] == 1


def _record(store, result=None):
    sid = store.record_prediction(
        asset="EURUSD_otc", expiry_s=60, signal="BUY", score=0.4, strength=0.7,
        agreement=0.8, regime="trend_up", data_sufficiency=1.0, entry_price=1.1,
        market_ts=time.time(), prediction_latency_ms=50.0, model_version="v",
        context={"sub_signals": [{"name": "trend_following", "direction": 1}]})
    if result:
        store.record_result(sid, result)
    return sid


def test_reset_display_hides_counter_but_keeps_data(tmp_path):
    store = SqliteSignalStore(tmp_path / "t.db")
    for _ in range(3):
        _record(store, "WIN")
    _record(store, "LOSS")
    assert store.stats()["total"] == 4

    hidden = store.reset_display()
    assert hidden == 4
    s = store.stats()
    assert s["total"] == 0 and s["wins"] == 0 and s["losses"] == 0   # UI shows zero
    assert s["display_reset_at"] is not None

    # ...but ALL data is preserved for learning
    assert len(store.all_for_training()) == 4
    assert len(store.losses_for_analysis()) == 1

    # new activity after reset shows in the counter again
    _record(store, "WIN")
    assert store.stats()["total"] == 1
    assert len(store.all_for_training()) == 5   # still counts everything


def test_set_analysis_and_meta(tmp_path):
    store = SqliteSignalStore(tmp_path / "m.db")
    sid = _record(store, "LOSS")
    store.set_analysis(sid, {"correct_direction": "DOWN", "misleading_signals": ["x"]})
    row = store.get_prediction(sid)
    assert json.loads(row["analysis_json"])["correct_direction"] == "DOWN"
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
