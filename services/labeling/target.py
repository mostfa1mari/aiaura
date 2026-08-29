"""Prediction target and reference-price methodology (Phase 6).

AI AURA predicts **directional movement over a chosen expiry** — the binary
outcome a Pocket Option option settles on: is the quote at expiry above
(BUY wins) or below (SELL wins) the quote at entry.

For an entry time ``T`` and horizon ``H``:

    effective_entry = T + entry_delay_s        (models execution latency)
    expiry          = effective_entry + H
    entry_price     = reference_price(effective_entry)
    expiry_price    = reference_price(expiry)
    outcome         = UP   if expiry_price > entry_price
                      DOWN if expiry_price < entry_price
                      FLAT if equal (at the configured precision)

Reference-price rule (documented in docs/PREDICTION_TARGET.md):
``reference_price(t)`` is the price of the **last tick at or before ``t``**
(the quote in effect at ``t``) — never a future tick. This mirrors how a
binary option reads the quote at a moment in time.

No look-ahead:
* ``entry_price`` uses only ticks with timestamp <= effective_entry.
* ``expiry_price`` uses only ticks with timestamp <= expiry.
* The label as a whole (which references the FUTURE expiry price) is a training
  TARGET, produced offline; it must never be fed to the feature engine, whose
  inputs are restricted to data at or before ``T``.

Honesty guarantees (no fabrication):
* If data does not bracket a reference point (no tick within staleness, or the
  series does not cover the timestamp), the label is marked ``valid=False``
  with a reason and MUST be excluded from training/backtests — never guessed.
* ``FLAT`` is a real outcome (common at short expiries / fine precision); it is
  reported, not hidden. On most binary platforms FLAT is a loss, but tie
  handling is left to the evaluator so it can be made explicit.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

from services.market_data.provider import CanonicalTick


class Direction(str, Enum):
    UP = "UP"      # BUY wins
    DOWN = "DOWN"  # SELL wins
    FLAT = "FLAT"  # no change at the configured precision


@dataclass(frozen=True)
class LabelConfig:
    horizon_s: float                          # expiry length in seconds
    entry_delay_s: float = 0.0                # execution latency; 0 = pure model eval
    max_reference_staleness_s: float = 2.0    # nearest prior tick must be this fresh
    price_precision: Optional[int] = None     # round to N decimals before compare; None = raw

    def __post_init__(self):
        if self.horizon_s <= 0:
            raise ValueError("horizon_s must be positive")
        if self.entry_delay_s < 0:
            raise ValueError("entry_delay_s must be >= 0")
        if self.max_reference_staleness_s < 0:
            raise ValueError("max_reference_staleness_s must be >= 0")


@dataclass(frozen=True)
class Label:
    entry_time: float                    # requested T (canonical UTC seconds)
    effective_entry_time: float          # T + entry_delay_s
    expiry_time: float                   # effective_entry_time + horizon_s
    entry_price: Optional[float]
    expiry_price: Optional[float]
    direction: Optional[Direction]
    valid: bool
    reason: str = ""

    @property
    def buy_wins(self) -> Optional[bool]:
        """True if a BUY would win, False if it would lose, None if invalid.
        FLAT counts as a loss for BUY (platform-typical); evaluators that treat
        ties differently should read ``direction`` directly."""
        if not self.valid or self.direction is None:
            return None
        return self.direction is Direction.UP


class ReferenceSeries:
    """Sorted price series with O(log n) 'quote in effect at t' lookup.

    Built once from a tick sequence, then queried for many entry times.
    """

    def __init__(self, ticks: Iterable[CanonicalTick]):
        # Sort by source_timestamp, then a DETERMINISTIC tiebreak
        # (received_timestamp, tick_id) so that among ticks sharing a
        # source_timestamp the last-arriving quote is chosen as "the quote in
        # effect" — regardless of input order (storage queries / merged sources
        # may not preserve arrival order). Keeps labels reproducible.
        keyed = [
            (t.source_timestamp, t.received_timestamp, t.tick_id, float(t.price))
            for t in ticks
            if _finite_pos(t.price)
        ]
        keyed.sort(key=lambda p: (p[0], p[1], p[2]))
        self._ts: List[float] = [p[0] for p in keyed]
        self._px: List[float] = [p[3] for p in keyed]

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def first_ts(self) -> Optional[float]:
        return self._ts[0] if self._ts else None

    @property
    def last_ts(self) -> Optional[float]:
        return self._ts[-1] if self._ts else None

    def price_at(self, t: float, max_staleness_s: float) -> Tuple[Optional[float], str]:
        """Quote in effect at ``t`` = last tick with ts <= t.

        Returns ``(price, "")`` on success, or ``(None, reason)`` when there is
        no tick at/before ``t``, the series does not cover ``t`` (t is after the
        last tick — the moment was not observed), or the nearest prior tick is
        older than ``max_staleness_s``.
        """
        if not self._ts:
            return None, "no ticks"
        if t < self._ts[0]:
            return None, "before first tick"
        if t > self._ts[-1]:
            # The series ends before t: the moment at t was never observed, so
            # we cannot know the quote then. Do NOT extrapolate.
            return None, "series does not cover timestamp (ends before it)"
        idx = bisect.bisect_right(self._ts, t) - 1
        age = t - self._ts[idx]
        if age > max_staleness_s:
            return None, f"nearest prior tick is stale ({age:.3f}s > {max_staleness_s}s)"
        return self._px[idx], ""


def _finite_pos(price) -> bool:
    import math
    return isinstance(price, (int, float)) and math.isfinite(price) and price > 0


def _round(price: float, precision: Optional[int]) -> float:
    return round(price, precision) if precision is not None else price


def make_label(series: ReferenceSeries, entry_time: float, config: LabelConfig) -> Label:
    """Label one entry against ``series`` using the reference-price rule."""
    eff_entry = entry_time + config.entry_delay_s
    expiry = eff_entry + config.horizon_s

    entry_price, entry_reason = series.price_at(eff_entry, config.max_reference_staleness_s)
    if entry_price is None:
        return Label(entry_time, eff_entry, expiry, None, None, None, False,
                     f"entry: {entry_reason}")

    expiry_price, expiry_reason = series.price_at(expiry, config.max_reference_staleness_s)
    if expiry_price is None:
        return Label(entry_time, eff_entry, expiry, entry_price, None, None, False,
                     f"expiry: {expiry_reason}")

    e = _round(entry_price, config.price_precision)
    x = _round(expiry_price, config.price_precision)
    if x > e:
        direction = Direction.UP
    elif x < e:
        direction = Direction.DOWN
    else:
        direction = Direction.FLAT

    return Label(entry_time, eff_entry, expiry, entry_price, expiry_price, direction, True, "")


def generate_labels(
    ticks_or_series,
    entry_times: Sequence[float],
    config: LabelConfig,
) -> List[Label]:
    """Label many entry times. ``ticks_or_series`` may be a ReferenceSeries or
    a tick iterable (a series is built once)."""
    series = ticks_or_series if isinstance(ticks_or_series, ReferenceSeries) else ReferenceSeries(ticks_or_series)
    return [make_label(series, t, config) for t in entry_times]


def infer_price_precision(ticks: Iterable[CanonicalTick], sample: int = 500) -> int:
    """Estimate quote decimal precision from observed tick prices (max decimals
    seen), useful as a default LabelConfig.price_precision. Returns 0 if none."""
    max_dec = 0
    seen = 0
    for t in ticks:
        if not _finite_pos(t.price):
            continue
        s = repr(float(t.price))
        if "e" in s or "E" in s:
            continue
        if "." in s:
            max_dec = max(max_dec, len(s.split(".", 1)[1]))
        seen += 1
        if seen >= sample:
            break
    return max_dec
