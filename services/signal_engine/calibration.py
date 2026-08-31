"""Confidence calibration — turn a raw conviction score into an HONEST win
probability, learned from the user's own settled outcomes.

The baseline/ML ``strength`` is a *conviction* number (how strongly the
indicators lean), NOT a probability. That is why a 100%-conviction signal can
lose and a 14%-conviction signal can win: conviction was never tied to real
outcomes. This module fixes that by estimating

    P(this emitted signal WINS | signals like it in the past)

from the settled WIN/LOSS history, with hierarchical Bayesian shrinkage so it
never overclaims on a tiny sample. With little data every estimate collapses
toward the honest base rate (and toward 0.5 when even that is thin); as real
outcomes accumulate, locality (per asset / expiry / conviction band) kicks in.

No look-ahead: it is built ONLY from already-settled predictions. The signal
being scored is not settled yet, so it can never calibrate on its own outcome.
This makes NO promise of future performance — it reports what actually happened
to comparable past signals, with an interval that is wide when evidence is thin.

Selection bias, stated honestly: going forward the only outcomes that get
recorded are the emitted (SIGNAL/EXPLORATORY) calls the user grades — WAITed
setups are never traded, so the settled sample is the gate's own selected calls,
not a representative sample of all scored signals. The decision layer guards
against this by refusing a *confident* SIGNAL unless the specific asset/expiry
cohort has its own real support (see decision.MIN_SUPPORT_FOR_SIGNAL); a coarse
global/expiry base rate alone can only produce an explicitly EXPLORATORY read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Calibrated:
    p: float                 # calibrated P(win), in [0, 1]
    low: float               # lower bound of the confidence interval
    high: float              # upper bound of the confidence interval
    support: int             # number of comparable settled signals behind `p`
    basis: str               # which cohort the estimate leaned on (for honesty)


def _wilson(p: float, n: float, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for a proportion. Well-behaved (stays in [0,1] and
    is honestly WIDE) for small n, unlike the normal approximation."""
    if n <= 0:
        return 0.0, 1.0
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


@dataclass(frozen=True)
class _Cohort:
    n: int
    wins: int
    strengths: Tuple[float, ...]      # strengths of members (for locality)
    results: Tuple[int, ...]          # 1 win / 0 loss, aligned with strengths


class Calibrator:
    """Built once from settled history; ``calibrate`` is cheap and pure.

    Shrinkage prior strengths (pseudo-counts) control how fast the estimate
    trusts a cohort over its more-general parent. Larger => more conservative.
    """

    def __init__(
        self,
        settled: Sequence[dict],
        *,
        k_global: float = 4.0,     # global shrinks toward 0.5 with this weight
        k_expiry: float = 8.0,     # expiry cohort shrinks toward global
        k_asset: float = 8.0,      # asset+expiry cohort shrinks toward expiry
        k_local: float = 6.0,      # conviction-band shrinks toward asset+expiry
        band: float = 0.15,        # strength half-width for the local cohort
        min_local: int = 5,        # need this many neighbors to use locality
    ):
        self.k_global = k_global
        self.k_expiry = k_expiry
        self.k_asset = k_asset
        self.k_local = k_local
        self.band = band
        self.min_local = min_local

        rows = [r for r in settled if str(r.get("result", "")).upper() in ("WIN", "LOSS")]
        self.n = len(rows)
        self.wins = sum(1 for r in rows if str(r["result"]).upper() == "WIN")
        self.base_rate = (self.wins / self.n) if self.n else 0.5

        # Index cohorts: by expiry, and by (asset, expiry).
        self._by_expiry: Dict[int, _Cohort] = {}
        self._by_asset_expiry: Dict[Tuple[str, int], _Cohort] = {}
        buckets_e: Dict[int, List[Tuple[float, int]]] = {}
        buckets_ae: Dict[Tuple[str, int], List[Tuple[float, int]]] = {}
        for r in rows:
            won = 1 if str(r["result"]).upper() == "WIN" else 0
            s = _as_float(r.get("strength"), 0.0)
            e = _as_int(r.get("expiry_s"), 0)
            a = str(r.get("asset", ""))
            buckets_e.setdefault(e, []).append((s, won))
            buckets_ae.setdefault((a, e), []).append((s, won))
        self._by_expiry = {e: _make_cohort(v) for e, v in buckets_e.items()}
        self._by_asset_expiry = {k: _make_cohort(v) for k, v in buckets_ae.items()}

    # -- shrinkage helpers --------------------------------------------------
    @staticmethod
    def _beta_mean(wins: float, n: float, prior_mean: float, prior_k: float) -> Tuple[float, float]:
        """Posterior mean of a Beta(prior) + Binomial(wins, n); returns (mean, pseudo_n)."""
        a = prior_mean * prior_k + wins
        b = (1.0 - prior_mean) * prior_k + (n - wins)
        total = a + b
        return (a / total if total > 0 else prior_mean), total

    def calibrate(self, strength: float, asset: str, expiry_s: int) -> Calibrated:
        # Tier 0: global, shrunk toward 0.5 so a tiny overall sample stays humble.
        g_mean, _ = self._beta_mean(self.wins, self.n, 0.5, self.k_global)

        # Tier 1: this expiry, shrunk toward global.
        ce = self._by_expiry.get(_as_int(expiry_s, 0))
        e_mean, _ = self._beta_mean(ce.wins if ce else 0, ce.n if ce else 0, g_mean, self.k_expiry)

        # Tier 2: this asset+expiry, shrunk toward expiry.
        cae = self._by_asset_expiry.get((str(asset), _as_int(expiry_s, 0)))
        ae_mean, _ = self._beta_mean(cae.wins if cae else 0, cae.n if cae else 0, e_mean, self.k_asset)

        # Tier 3: conviction-band locality within asset+expiry, shrunk toward Tier 2.
        support = cae.n if cae else 0
        basis = _basis_of(cae, ce, self.n)
        p = ae_mean
        if cae and cae.n >= self.min_local:
            local = _neighbors(cae, strength, self.band)
            if local[1] >= self.min_local:  # (wins, n)
                lw, ln = local
                p, _ = self._beta_mean(lw, ln, ae_mean, self.k_local)
                support = ln
                basis = f"asset+expiry, conviction≈{strength:.2f} ({ln} neighbors)"

        # Interval is sized by the REAL evidence count, not the prior-inflated
        # pseudo-count, so thin samples read as honestly wide (support 0 -> [0,1])
        # and cannot masquerade as high-confidence. The point estimate p stays the
        # shrunken posterior mean; the band is centered on it but widened to the
        # uncertainty the real support warrants.
        low, high = _wilson(p, float(support))
        return Calibrated(p=p, low=low, high=high, support=int(support), basis=basis)


# --- module helpers ---------------------------------------------------------

def _as_float(x, default: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _as_int(x, default: int) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _make_cohort(pairs: List[Tuple[float, int]]) -> _Cohort:
    strengths = tuple(p[0] for p in pairs)
    results = tuple(p[1] for p in pairs)
    return _Cohort(n=len(pairs), wins=sum(results), strengths=strengths, results=results)


def _neighbors(cohort: _Cohort, strength: float, band: float) -> Tuple[int, int]:
    """(wins, n) among members whose conviction is within `band` of `strength`.

    Known limitation (low impact): `strength` is defined per active model, so a
    cohort can pool convictions produced by different champions/baseline on one
    un-segmented scale. Shrinkage toward the asset+expiry mean (k_local) bounds
    the effect; segmenting neighbors by model_version would remove it entirely.
    """
    wins = n = 0
    for s, won in zip(cohort.strengths, cohort.results):
        if abs(s - strength) <= band:
            n += 1
            wins += won
    return wins, n


def _basis_of(cae: Optional[_Cohort], ce: Optional[_Cohort], n_global: int) -> str:
    if cae and cae.n:
        return f"asset+expiry ({cae.n} settled)"
    if ce and ce.n:
        return f"expiry ({ce.n} settled)"
    if n_global:
        return f"overall base rate ({n_global} settled)"
    return "no settled history yet"
