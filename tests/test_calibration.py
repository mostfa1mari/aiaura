"""Calibration tests — the honest-confidence layer.

Verifies: humble on tiny/empty data, moves toward observed win rate with
evidence, never returns a degenerate 0/1, respects cohort locality, and the
Wilson interval brackets the estimate and widens as support shrinks.
"""

from services.signal_engine.calibration import Calibrator, _wilson


def _rows(n_win, n_loss, asset="EURUSD_otc", expiry=60, strength=0.5):
    rows = []
    for _ in range(n_win):
        rows.append({"result": "WIN", "asset": asset, "expiry_s": expiry, "strength": strength})
    for _ in range(n_loss):
        rows.append({"result": "LOSS", "asset": asset, "expiry_s": expiry, "strength": strength})
    return rows


def test_empty_history_is_maximally_humble():
    c = Calibrator([])
    est = c.calibrate(0.9, "EURUSD_otc", 60)
    assert abs(est.p - 0.5) < 1e-9          # nothing known -> 0.5
    assert est.support == 0
    # Interval must reflect ZERO evidence -> maximally wide, not a prior-narrowed band.
    assert est.low == 0.0 and est.high == 1.0


def test_interval_sized_by_real_support_not_prior():
    # 5 winning neighbors should NOT read as a tight, near-certain band.
    c = Calibrator(_rows(5, 0))
    est = c.calibrate(0.5, "EURUSD_otc", 60)
    assert est.support == 5
    # A real 5/5 Wilson lower bound is well under 1.0 (honest thin-sample width).
    assert est.low < 0.75
    assert est.high <= 1.0


def test_all_wins_shrinks_below_one():
    # 6 wins, 0 losses: honest estimate is high but NEVER 1.0 (shrinkage).
    c = Calibrator(_rows(6, 0))
    est = c.calibrate(0.5, "EURUSD_otc", 60)
    assert 0.5 < est.p < 1.0
    assert est.high <= 1.0 and est.low >= 0.0


def test_all_losses_shrinks_above_zero():
    c = Calibrator(_rows(0, 6))
    est = c.calibrate(0.5, "EURUSD_otc", 60)
    assert 0.0 < est.p < 0.5


def test_moves_toward_observed_rate_with_more_evidence():
    small = Calibrator(_rows(7, 3))          # 70% over 10
    big = Calibrator(_rows(70, 30))          # 70% over 100
    es = small.calibrate(0.5, "EURUSD_otc", 60)
    eb = big.calibrate(0.5, "EURUSD_otc", 60)
    # Both lean >0.5; the larger sample sits closer to the true 0.70 and is tighter.
    assert eb.p > es.p
    assert (eb.high - eb.low) < (es.high - es.low)


def test_cohort_locality_separates_assets():
    rows = _rows(20, 0, asset="AUDCAD_otc", expiry=60) + _rows(0, 20, asset="EURUSD_otc", expiry=60)
    c = Calibrator(rows)
    good = c.calibrate(0.5, "AUDCAD_otc", 60)
    bad = c.calibrate(0.5, "EURUSD_otc", 60)
    assert good.p > 0.6 and bad.p < 0.4      # each cohort pulls its own way


def test_unknown_cohort_falls_back_to_base_rate():
    # History only for EURUSD; a brand-new asset leans on the global base rate.
    c = Calibrator(_rows(8, 2, asset="EURUSD_otc", expiry=60))
    est = c.calibrate(0.5, "ZZZ_otc", 5)
    assert 0.5 < est.p < 0.8                 # ~ global 0.8 rate, shrunk toward 0.5
    assert "base rate" in est.basis or "expiry" in est.basis


def test_wilson_widens_as_n_shrinks():
    lo1, hi1 = _wilson(0.5, 5)
    lo2, hi2 = _wilson(0.5, 100)
    assert (hi1 - lo1) > (hi2 - lo2)
    assert 0.0 <= lo1 <= hi1 <= 1.0


def test_ignores_pending_rows():
    rows = _rows(5, 0) + [{"result": None, "asset": "EURUSD_otc", "expiry_s": 60, "strength": 0.5}]
    c = Calibrator(rows)
    assert c.n == 5                          # pending not counted
