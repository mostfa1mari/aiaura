"""Decision — always a directional BUY/SELL, graded by honest confidence.

The app ALWAYS gives a clear BUY/SELL the user can act on and grade; it never
hides the call behind a WAIT. Honesty lives in the CALIBRATED confidence (learned
from the user's own settled outcomes) and a tier label:

  * strong   — calibrated on enough history, win chance beats the payout
               break-even (1/(1+payout/100): 92% -> 52.1% needed, 71% -> 58.5%),
               and the indicators/strategies/similarity agree.
  * moderate — win chance at/above break-even but not a standout.
  * low      — win chance below break-even, or too little history yet. Still
               shown (with the honest number) so the user decides; grading it
               feeds back into the calibration for that asset.

This never promises a win and never guarantees avoiding losing streaks — no such
guarantee is possible on broker OTC. It reports honest odds and always lets the
user trade and report the outcome so the confidence keeps getting more accurate.
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
    """ALWAYS returns a directional call (BUY/SELL) so the user gets a clear,
    gradeable signal every time. Honesty lives in the calibrated confidence and
    the tier (strong / moderate / low), NOT in blocking the trade. The number
    updates from the user's reported outcomes, so grading losses genuinely lowers
    the confidence on that asset over time."""
    be = break_even(payout)
    conf = calibrated.p
    edge = conf - be
    confl = _confluence(side, strategies, similarity)

    # Tier drives styling + guidance only — never suppression.
    calibrated_enough = calibrated.support >= MIN_SUPPORT_FOR_SIGNAL
    if calibrated_enough and conf >= be + EDGE_MARGIN and confl >= MIN_CONFLUENCE:
        tier = "strong"
    elif conf >= be:
        tier = "moderate"
    else:
        tier = "low"

    return Decision(
        decision="SIGNAL", side=side,
        confidence=conf, confidence_low=calibrated.low, confidence_high=calibrated.high,
        support=calibrated.support, basis=calibrated.basis, break_even=be, edge=edge,
        tier=tier, confluence=confl,
        reasons=[_reason(tier, conf, be, calibrated, confl)],
    )


def _reason(tier: str, conf: float, be: float, calibrated: Calibrated, confl: int) -> str:
    pct, bep = round(conf * 100), round(be * 100)
    if tier == "strong":
        return (f"Strong: {pct}% calibrated win chance vs {bep}% break-even, "
                f"{confl}/3 signals agree, from {calibrated.support} past outcomes.")
    if tier == "moderate":
        return (f"Moderate: {pct}% calibrated win chance vs {bep}% needed to profit. "
                f"Tradeable but not a standout.")
    if calibrated.support:
        return (f"Low confidence: {pct}% win chance is below the {bep}% needed at this "
                f"payout — size small or skip.")
    return (f"Low confidence: still learning this asset — grade the outcome so the "
            f"win chance here gets accurate.")
