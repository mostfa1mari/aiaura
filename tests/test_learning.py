"""Learning-engine tests: training honesty, registry, self-learning gate."""

import random

from services.learning_engine.dataset import DatasetRow, build_dataset
from services.learning_engine.registry import ModelRecord, ModelRegistry
from services.learning_engine.self_learning import (
    _is_promotable,
    detect_drift,
    run_training_cycle,
)
from services.learning_engine.train import train_and_select
from services.market_data.provider import CanonicalCandle

BASE = 1787000000
TF = 60


class _Dummy:
    """Picklable stand-in model for registry tests."""
    def predict(self, X):
        return [1] * len(X)


def candle(i, c, o):
    return CanonicalCandle(asset="EURUSD_otc", timeframe_s=TF, timestamp=float(BASE + i * TF),
                           open=o, high=max(o, c) + 1e-4, low=min(o, c) - 1e-4, close=c,
                           tick_count=10, complete=True, provider="pocket_option")


def test_trainer_learns_a_real_signal():
    random.seed(1)
    rows = []
    for i in range(800):
        x = random.uniform(-1, 1)
        feats = [x, random.uniform(-1, 1), random.uniform(-1, 1)]
        rows.append(DatasetRow(BASE + i, feats, 1 if x > 0 else 0))  # label determined by x
    out = train_and_select(rows, ["a", "b", "c"], payout=0.8, min_train=150)
    assert out is not None
    assert out.metrics["held_out_gate"] is True       # judged on unseen data
    assert out.metrics["oos_win_rate"] > 0.9          # the signal is learnable
    assert out.metrics["oos_expectancy"] > 0
    assert _is_promotable(out.metrics, None) is True


def test_trainer_refuses_to_claim_edge_on_noise():
    random.seed(2)
    rows = [DatasetRow(BASE + i, [random.uniform(-1, 1) for _ in range(3)],
                       random.randint(0, 1)) for i in range(800)]
    out = train_and_select(rows, ["a", "b", "c"], payout=0.8, min_train=150)
    # a model may fit, but it must NOT be promotable (no significant edge)
    assert out is None or _is_promotable(out.metrics, None) is False


def test_trainer_none_on_single_class():
    rows = [DatasetRow(BASE + i, [0.1, 0.2], 1) for i in range(300)]
    assert train_and_select(rows, ["a", "b"]) is None


def test_registry_save_load_promote_rollback(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    rec1 = ModelRecord("v1", "fv", "logistic", 1.0, [0, 1], 100, 20, {"oos_expectancy": 0.1})
    reg.save(_Dummy(), rec1, make_champion=True)
    assert reg.champion_version == "v1"
    assert reg.load_champion().predict([[1]]) == [1]

    rec2 = ModelRecord("v2", "fv", "random_forest", 2.0, [0, 1], 120, 25, {"oos_expectancy": 0.2})
    reg.save(_Dummy(), rec2, make_champion=False)   # challenger, not deployed
    assert reg.champion_version == "v1"
    reg.promote("v2")
    assert reg.champion_version == "v2"
    reg.promote("v1")                                # rollback
    assert reg.champion_version == "v1"
    assert len(reg.records()) == 2


def test_promotion_gate_requires_held_out_significance_and_margin():
    G = {"held_out_gate": True}
    weak = {**G, "oos_expectancy": -0.01, "p_value_one_sided": 0.4}
    strong = {**G, "oos_expectancy": 0.15, "p_value_one_sided": 0.001}
    # non-held-out metrics are never promotable (winner's-curse guard)
    assert _is_promotable({"oos_expectancy": 0.5, "p_value_one_sided": 0.0}, None) is False
    assert _is_promotable(weak, None) is False
    assert _is_promotable(strong, None) is True
    # p just above the strict 0.01 threshold must fail
    assert _is_promotable({**G, "oos_expectancy": 0.15, "p_value_one_sided": 0.02}, None) is False
    champ = ModelRecord("c", "fv", "k", 1.0, [0, 1], 100, 20, {"oos_expectancy": 0.2})
    assert _is_promotable(strong, champ) is False                      # no margin over champ
    tiny_gain = {**G, "oos_expectancy": 0.21, "p_value_one_sided": 0.001}
    assert _is_promotable(tiny_gain, champ) is False                   # < 0.02 margin
    better = {**G, "oos_expectancy": 0.25, "p_value_one_sided": 0.001}
    assert _is_promotable(better, champ) is True


def test_self_learning_cycle_is_honest_on_noise(tmp_path):
    random.seed(3)
    out, p = [], 1.10
    for i in range(700):
        p = round(p + random.choice([-0.001, 0.001]), 6)
        out.append(candle(i, p, p))
    reg = ModelRegistry(tmp_path / "models")
    res = run_training_cycle(out, horizon_s=TF, registry=reg, payout=0.8, now=1.0)
    assert res["status"] in ("trained", "no_trainable_model", "insufficient_data")
    if res["status"] == "trained":
        assert res["promoted"] is False       # random walk -> no edge -> not deployed
        assert reg.champion_version is None    # nothing deployed


def test_detect_drift():
    assert detect_drift(5, 10, 0.6)["status"] == "insufficient"
    assert detect_drift(20, 100, 0.6)["status"] == "drift"     # 20% vs 60% expected
    assert detect_drift(62, 100, 0.6)["status"] == "ok"


def test_build_dataset_excludes_flat_and_is_two_class(tmp_path):
    # alternating up/down closes -> both labels present, no FLAT
    rows_out = []
    p = 1.10
    for i in range(200):
        p = round(p + (0.001 if i % 2 == 0 else -0.0008), 6)
        rows_out.append(candle(i, p, p))
    rows, names = build_dataset(rows_out, horizon_s=TF, warmup=50)
    assert len(rows) > 0
    labels = {r.label for r in rows}
    assert labels == {0, 1}
    assert len(rows[0].features) == len(names)
