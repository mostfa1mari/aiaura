"""Decision gate — SIGNAL, EXPLORATORY, or WAIT.

A disciplined trader does not take every setup, and does not pretend to know
odds it hasn't measured. This gate has three outcomes:

  * SIGNAL      — enough comparable settled outcomes to CALIBRATE this setup AND
                  the calibrated win chance clears the payout break-even (with a
                  margin) AND the indicators/strategies/similarity confirm it.
                  Break-even for a binary paying `payout`% is 1/(1+payout/100):
                  92% -> 52.1% needed, 71% -> 58.5% needed. Below that a trade is
                  negative expected value no matter how "strong" it looks.
  * EXPLORATORY — the direction is confirmed by confluence, but there isn't yet
                  enough history for THIS asset/expiry to calibrate a confident
                  probability. Emitted as an explicitly UNCALIBRATED, gradeable
                  read so the system can gather its first outcomes and learn.
                  Never presented as a confident win probability.
  * WAIT        — no confirmation, not enough data/latency, or a calibrated setup
                  whose honest odds don't beat the payout. Nothing to trade.

This never promises a win. A confident SIGNAL requires real, asset-specific
evidence — it can't ride an optimistic global average into an emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from services.signal_engine.calibration import Calibrated


# Never surface a *confident* signal under 50% calibrated win prob (the user's
# floor), even when a low payout would make <50% "break-even".
MIN_CONFIDENCE = 0.50
# Cushion over break-even so borderline +EV setups still wait.
EDGE_MARGIN = 0.02
# Need at least this share of the recent candles to act at all.
MIN_DATA_SUFFICIENCY = 0.80
# Independent confirmations required (of: baseline, strategy ensemble, similarity).
MIN_CONFLUENCE = 2
# Comparable settled outcomes needed before a CALIBRATED (confident) SIGNAL is
# allowed. Below this the setup can only be EXPLORATORY, never a confident call.
MIN_SUPPORT_FOR_SIGNAL = 8

# Historical-similarity reports direction as UP/DOWN; map to the trade side.
_LEANS_TO_SIDE = {"UP": "BUY", "DOWN": "SELL"}


@dataclass(frozen=True)
class Decision:
    decision: str                 # "SIGNAL" | "EXPLORATORY" | "WAIT"
    side: Optional[str]           # "BUY" | "SELL" | None (WAIT)
    confidence: float             # calibrated P(win)
    confidence_low: float
    confidence_high: float
    support: int                  # settled signals behind the estimate
    basis: str
    break_even: float             # win rate needed to be +EV at this payout
    edge: float                   # confidence - break_even
    tier: str                     # "strong" | "moderate" | "exploratory" | "wait"
    confluence: int               # independent confirmations on `side`
    reasons: List[str] = field(default_factory=list)


def break_even(payout: Optional[float]) -> float:
    """Win rate needed to break even. Unknown payout -> assume a hard 0.55."""
    if payout is None or payout <= 0:
        return 0.55
    return 1.0 / (1.0 + payout / 100.0)


def _confluence(side: str, strategies: Optional[dict], similarity: Optional[dict]) -> int:
    """Count independent components that CONFIRM `side` (baseline itself is the
    1st confirmation, always counted since `side` comes from it)."""
    n = 1  # the indicator/ML baseline that produced `side`
    if strategies and strategies.get("signal") == side and (strategies.get("contributors") or 0) > 0:
        # A net-zero ensemble is undecided (its tie defaults to BUY) — not a
        # confirmation. Only count it when it is directionally committed.
        score = strategies.get("score")
        if score is None or abs(score) > 0:
            n += 1
    if similarity and similarity.get("confident"):
        leans = str(similarity.get("leans", "")).upper()
        if _LEANS_TO_SIDE.get(leans) == side:   # UP->BUY, DOWN->SELL
            n += 1
    return n


def decide(
    *,
    side: str,
    calibrated: Calibrated,
    payout: Optional[float],
    data_sufficiency: float,
    strategies: Optional[dict] = None,
    similarity: Optional[dict] = None,
    latency_viability: Optional[dict] = None,
) -> Decision:
    be = break_even(payout)
    required = max(MIN_CONFIDENCE, be + EDGE_MARGIN)
    conf = calibrated.p
    edge = conf - be
    confl = _confluence(side, strategies, similarity)

    def mk(decision: str, tier: str, reasons: List[str]) -> Decision:
        return Decision(
            decision=decision, side=(side if decision != "WAIT" else None),
            confidence=conf, confidence_low=calibrated.low, confidence_high=calibrated.high,
            support=calibrated.support, basis=calibrated.basis, break_even=be, edge=edge,
            tier=tier, confluence=confl, reasons=reasons,
        )

    # Structural blockers — independent of how much calibration history exists.
    blockers: List[str] = []
    if data_sufficiency < MIN_DATA_SUFFICIENCY:
        blockers.append("not enough recent candles to be sure yet")
    if confl < MIN_CONFLUENCE:
        blockers.append("the strategies don't agree strongly enough")
    if latency_viability and str(latency_viability.get("verdict", "")).lower() in (
        "too_slow", "infeasible", "not_viable"):
        blockers.append("the signal is too slow to enter for this expiry")
    if blockers:
        return mk("WAIT", "wait", blockers)

    # Enough comparable outcomes to make a CALIBRATED, confident judgment.
    if calibrated.support >= MIN_SUPPORT_FOR_SIGNAL:
        if conf >= required:
            tier = "strong" if calibrated.low >= be else "moderate"
            return mk("SIGNAL", tier, [_positive_reason(tier, conf, be, calibrated)])
        if conf < MIN_CONFIDENCE:
            reason = f"confidence {conf*100:.0f}% is below the 50% floor"
        else:
            reason = (f"confidence {conf*100:.0f}% doesn't clear the {required*100:.0f}% "
                      f"needed (break-even {be*100:.0f}% + {EDGE_MARGIN*100:.0f}% margin)")
        return mk("WAIT", "wait", [reason])

    # Not enough comparable outcomes to calibrate THIS setup: emit an explicitly
    # UNCALIBRATED, gradeable exploratory read so the system can accumulate its
    # first per-setup outcomes. This is NOT a confident probability claim.
    return mk("EXPLORATORY", "exploratory", [
        f"No track record for this asset/expiry yet ({calibrated.support} comparable "
        f"outcome{'' if calibrated.support == 1 else 's'}). This is the model's raw "
        f"directional read — grade it so future signals here can be calibrated."])


def _positive_reason(tier: str, conf: float, be: float, calibrated: Calibrated) -> str:
    lead = "High-confidence setup" if tier == "strong" else "Setup clears the payout math"
    lo, hi = round(calibrated.low * 100), round(calibrated.high * 100)
    basis = (f", from {calibrated.support} comparable past outcomes" if calibrated.support else "")
    return (f"{lead}: {conf*100:.0f}% (range {lo}–{hi}%) calibrated win chance, "
            f"needs {be*100:.0f}% to profit{basis}.")
