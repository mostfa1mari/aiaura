"""Build a supervised dataset from candles (no look-ahead).

For each closed candle i (after warmup) we compute features from candles[:i+1]
and the directional outcome over ``horizon_s`` using future closes. Features
use only the past; the label is the future outcome (the target). FLAT outcomes
are excluded (ambiguous for a binary UP/DOWN classifier).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from services.feature_engine import compute_features, feature_names
from services.market_data.provider import CanonicalCandle


@dataclass
class DatasetRow:
    timestamp: float           # entry candle time (for chronological ordering)
    features: List[float]      # dense, in feature_names() order
    label: int                 # 1 = UP (BUY wins), 0 = DOWN (SELL wins)


def _close_ref(candles: Sequence[CanonicalCandle]):
    pts = sorted((c.timestamp, c.close) for c in candles)
    ts = [p[0] for p in pts]
    px = [p[1] for p in pts]

    def price_at(t: float) -> Optional[float]:
        if not ts or t < ts[0] or t > ts[-1]:
            return None
        return px[bisect.bisect_right(ts, t) - 1]

    return price_at


def build_dataset(
    candles: Sequence[CanonicalCandle],
    horizon_s: float,
    warmup: int = 50,
    price_precision: Optional[int] = None,
) -> Tuple[List[DatasetRow], List[str]]:
    """Return (rows sorted by time, feature_name order). Empty when too short."""
    closed = sorted((c for c in candles if c.complete), key=lambda c: c.timestamp)
    names = feature_names()
    rows: List[DatasetRow] = []
    if len(closed) <= warmup:
        return rows, names

    price_at = _close_ref(closed)
    for i in range(warmup, len(closed)):
        entry = closed[i]
        entry_price = price_at(entry.timestamp)
        expiry_price = price_at(entry.timestamp + horizon_s)
        if entry_price is None or expiry_price is None:
            continue
        e = round(entry_price, price_precision) if price_precision is not None else entry_price
        x = round(expiry_price, price_precision) if price_precision is not None else expiry_price
        if x == e:
            continue  # FLAT excluded
        label = 1 if x > e else 0
        fv = compute_features(closed[: i + 1])
        rows.append(DatasetRow(entry.timestamp, fv.as_row(names), label))
    return rows, names
