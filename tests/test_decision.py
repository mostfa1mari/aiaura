"""Decision-gate tests — SIGNAL / EXPLORATORY / WAIT.

Verifies the payout break-even math, the sub-50% floor, the confluence and
data/latency gates, the support floor that keeps a confident SIGNAL from riding
a coarse base rate, the EXPLORATORY bootstrap path, and honest reason text.
"""

from services.signal_engine.calibration import Calibrated
from services.signal_engine.decision import (
    break_even, decide, MIN_CONFIDENCE, MIN_SUPPORT_FOR_SIGNAL,
)


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


def test_emits_strong_signal_when_everything_aligns():
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL"
    assert d.side == "BUY"
    assert d.tier == "strong"                          # low (0.60) >= break-even
    assert d.confluence == 3


def test_waits_when_confidence_below_break_even():
    # 53% would beat 50% but NOT the 58.5% needed at 71% payout. Support is high
    # so this is a CALIBRATED wait, not exploratory.
    d = decide(side="BUY", calibrated=cal(0.53, support=30), payout=71,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "WAIT"
    assert any("clear" in r or "needed" in r for r in d.reasons)


def test_waits_below_fifty_floor_even_if_payout_low():
    d = decide(side="BUY", calibrated=cal(0.47, support=30), payout=400,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "WAIT"
    assert d.confidence < MIN_CONFIDENCE


def test_waits_when_strategies_disagree():
    d = decide(side="BUY", calibrated=cal(0.72, low=0.62, support=30), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "SELL", "contributors": 5, "score": -0.8},
               similarity={"leans": "DOWN", "confident": True},
               latency_viability=FAST)
    assert d.decision == "WAIT"                         # confluence == 1 (baseline only)
    assert d.confluence == 1


def test_waits_when_data_insufficient():
    d = decide(side="BUY", calibrated=cal(0.72, low=0.62, support=30), payout=92,
               data_sufficiency=0.5, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "WAIT"


def test_waits_when_latency_too_slow():
    d = decide(side="BUY", calibrated=cal(0.72, low=0.62, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability={"verdict": "too_slow", "fraction": 1.4})
    assert d.decision == "WAIT"


def test_moderate_tier_when_lower_bound_below_break_even():
    d = decide(side="BUY", calibrated=cal(0.60, low=0.48, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "SIGNAL"
    assert d.tier == "moderate"


def test_similarity_up_confirms_buy_as_second_confluence():
    # Strategy ensemble is silent; similarity UP must map to BUY and provide the
    # 2nd confirmation so a strong, confirmed BUY still emits.
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "SELL", "contributors": 0, "score": 0.0},   # not confirming
               similarity={"leans": "UP", "confident": True},
               latency_viability=FAST)
    assert d.confluence == 2
    assert d.decision == "SIGNAL"


def test_net_zero_strategy_ensemble_is_not_a_confirmation():
    # A balanced (net-zero) ensemble defaults its label to BUY, but must NOT
    # count as a confirmation — otherwise BUY gets a spurious confluence.
    d = decide(side="BUY", calibrated=cal(0.70, low=0.60, support=30), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "BUY", "contributors": 2, "score": 0.0},
               similarity=None, latency_viability=FAST)
    assert d.confluence == 1
    assert d.decision == "WAIT"


def test_exploratory_when_support_below_floor():
    # Confirmed direction but not enough comparable history to calibrate:
    # emit a gradeable EXPLORATORY read, never a confident SIGNAL.
    d = decide(side="BUY", calibrated=cal(0.62, support=MIN_SUPPORT_FOR_SIGNAL - 1),
               payout=92, data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "EXPLORATORY"
    assert d.side == "BUY"
    assert d.tier == "exploratory"


def test_zero_support_cannot_emit_confident_signal():
    # A brand-new asset+expiry (support 0) whose coarse base rate looks good must
    # NOT be emitted as a confident SIGNAL on inherited evidence.
    d = decide(side="BUY", calibrated=cal(0.60, low=0.55, support=0), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "EXPLORATORY"


def test_exploratory_still_requires_confluence():
    d = decide(side="BUY", calibrated=cal(0.62, support=2), payout=92,
               data_sufficiency=1.0,
               strategies={"signal": "SELL", "contributors": 3, "score": -0.5},
               similarity={"leans": "DOWN", "confident": True},
               latency_viability=FAST)
    assert d.decision == "WAIT"                          # no confirmation -> not even exploratory


def test_wait_reason_names_the_real_threshold():
    # payout 92 -> be 52%, required 54%; conf 53% clears be but not required.
    d = decide(side="BUY", calibrated=cal(0.53, support=30), payout=92,
               data_sufficiency=1.0, strategies=STRAT_BUY, similarity=SIM_BUY,
               latency_viability=FAST)
    assert d.decision == "WAIT"
    joined = " ".join(d.reasons)
    assert "54%" in joined and "margin" in joined       # states the enforced threshold, not just be
