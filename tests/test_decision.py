"""Decision tests — always a directional BUY/SELL, tiered by honest confidence.

The app never blocks a call; it grades it strong / moderate / low. Verifies the
break-even math, the tier boundaries, confluence handling (similarity UP/DOWN
mapping, net-zero ensemble), and that a signal is ALWAYS emitted and gradeable.
"""

from services.signal_engine.calibration import Calibrated
from services.signal_engine.decision import break_even, decide, MIN_SUPPORT_FOR_SIGNAL


def cal(p, low=None, high=None, support=30):
    low = p - 0.05 if low is None else low
    high = p + 0.05 if high is None else high
    return Calibrated(p=p, low=low, high=high, support=support, basis="test")


STRAT_BUY = {"signal": "BUY", "agreement": 0.9, "contributors": 5, "score": 0.8}
SIM_BUY = {"leans": "UP", "confident": True, "n_neighbors": 30, "directional_rate": 0.7}  # UP -> BUY
FAST = {"verdict": "viable", "fraction": 0.1}


def test_break_even_matches_payout_math():
    assert abs(break_even(92) - 1 / 1.92) < 1e-6      # ~0.5208
    assert abs(break_even(71) - 1 / 1.71) < 1e-6      # ~0.5848
    assert break_even(None) == 0.55
    assert break_even(0) == 0.55


def test_always_emits_a_directional_signal():
    # Even with terrible confidence, no confluence, thin data: still a BUY/SELL.
    d = decide(side="SELL", calibrated=cal(0.20, support=0), payout=71,
               data_sufficiency=0.1, strategies=None, similarity=None,
               latency_viability={"verdict": "too_slow"})
    assert d.decision == "SIGNAL"
    assert d.side == "SELL"


def test_strong_when_calibrated_confident_and_confirmed():
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL" and d.tier == "strong"
    assert d.confluence == 3


def test_moderate_when_just_above_break_even():
    # 53% >= 52.1% break-even but < break-even+2% margin -> moderate, not strong.
    d = decide(side="BUY", calibrated=cal(0.53, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL" and d.tier == "moderate"


def test_low_when_below_break_even():
    d = decide(side="BUY", calibrated=cal(0.45, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL" and d.tier == "low"


def test_thin_history_cannot_be_strong():
    # High confidence but too few outcomes -> not "strong" (support gate on tier).
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=MIN_SUPPORT_FOR_SIGNAL - 1),
               payout=92, data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL"
    assert d.tier == "moderate"                     # >= break-even but not calibrated enough
    assert d.tier != "strong"


def test_low_no_history_reason_mentions_learning():
    d = decide(side="BUY", calibrated=cal(0.44, support=0), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.tier == "low"
    assert any("learning" in r for r in d.reasons)


def test_confluence_needs_similarity_up_mapping_for_strong():
    # Strategy silent; similarity UP must map to BUY to reach confluence 2 (strong).
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "SELL", "contributors": 0, "score": 0.0},
               similarity={"leans": "UP", "confident": True},
               latency_viability=FAST)
    assert d.confluence == 2 and d.tier == "strong"


def test_net_zero_strategy_ensemble_is_not_a_confirmation():
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "BUY", "contributors": 2, "score": 0.0},
               similarity=None, latency_viability=FAST)
    assert d.confluence == 1
    assert d.tier == "moderate"                     # confirmed conf but confluence<2 -> not strong


def test_low_payout_needs_higher_confidence_to_clear():
    # 55% clears 52% (92% payout) but not 58.5% (71% payout).
    hi_payout = decide(side="BUY", calibrated=cal(0.55, support=30), payout=92,
                       data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
                       latency_viability=FAST)
    lo_payout = decide(side="BUY", calibrated=cal(0.55, support=30), payout=71,
                       data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
                       latency_viability=FAST)
    assert hi_payout.tier in ("strong", "moderate")
    assert lo_payout.tier == "low"                  # 55% < 58.5% break-even
