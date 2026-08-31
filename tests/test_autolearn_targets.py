"""Auto-learner target selection: dynamically train the high-payout pairs."""

from dataclasses import dataclass

from services.learning_engine.auto import AutoLearnConfig, AutoLearner, _summarize


@dataclass
class _Info:
    payout: float


class _Provider:
    def __init__(self, assets):
        self._assets = assets

    def get_assets(self):
        return self._assets

    def is_connected(self):
        return True


def _learner(provider, **cfg_kw):
    cfg = AutoLearnConfig(**cfg_kw)
    # registry_dir is only touched on training; target selection never trains.
    return AutoLearner(provider, store=None, registry_dir="/tmp/none",
                       on_promote=lambda v: None, config=cfg)


def test_dynamic_targets_selects_high_payout_only():
    provider = _Provider({
        "EURUSD_otc": _Info(92), "GBPUSD_otc": _Info(92), "AUDCAD_otc": _Info(90),
        "USDZAR_otc": _Info(60), "EXOTIC_otc": _Info(45),
    })
    al = _learner(provider, payout_threshold=90.0, target_expiries=[60])
    targets = al._current_targets()
    syms = {a for a, _ in targets}
    assert syms == {"EURUSD_otc", "GBPUSD_otc", "AUDCAD_otc"}   # >=90 only
    assert all(exp == 60 for _, exp in targets)


def test_dynamic_targets_multiple_expiries_and_cap():
    provider = _Provider({f"P{i}_otc": _Info(92) for i in range(30)})
    al = _learner(provider, payout_threshold=90.0, target_expiries=[60, 15], max_targets=10)
    targets = al._current_targets()
    assert len(targets) == 10                                   # capped


def test_dynamic_falls_back_to_static_when_no_high_payout():
    provider = _Provider({"LOW_otc": _Info(50)})
    al = _learner(provider, payout_threshold=90.0)
    al._cfg.targets = [("EURUSD_otc", 60)]
    assert al._current_targets() == [("EURUSD_otc", 60)]


def test_static_mode_ignores_catalog():
    provider = _Provider({"EURUSD_otc": _Info(92)})
    al = _learner(provider, dynamic_high_payout=False)
    al._cfg.targets = [("GBPUSD_otc", 15)]
    assert al._current_targets() == [("GBPUSD_otc", 15)]


def test_summarize_reports_pass_breadth():
    results = [
        {"status": "trained", "promoted": False, "version": "a-rc", "metrics": {"oos_expectancy": -0.1}},
        {"status": "trained", "promoted": False, "version": "b-rc", "metrics": {"oos_expectancy": 0.05}},
        {"status": "insufficient_data"},
    ]
    s = _summarize(results, reason="scheduled", n_targets=3)
    assert s["targets_trained"] == 2 and s["targets_total"] == 3
    assert s["promoted_count"] == 0
    assert s["version"] == "b-rc"                               # strongest challenger surfaced
