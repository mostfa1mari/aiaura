"""Candle engine (Phase 5).

Builds OHLC candles from a canonical tick stream for the AI AURA horizons:

    1s, 3s, 5s, 10s, 15s, 30s, 1m, 3m, 5m, 15m

Some of these (notably 3s) are not offered by Pocket Option's history API
(see docs/POCKET_OPTION_API_AUDIT.md §6), so they must be constructed from
ticks — which is exactly what this engine does.

Rules (docs/DATA_SCHEMA.md, docs/DATA_QUALITY.md):

* A candle's bucket is ``floor(source_timestamp / tf) * tf`` (UTC seconds).
* OHLC uses ticks ordered by ``source_timestamp``: open = first, close =
  last, high/low = max/min; ``tick_count`` = number of ticks in the bucket.
* NO fabrication: a bucket with no ticks produces NO candle. Gaps are real
  and must be handled by consumers, never forward-filled here.
* The still-forming bucket is marked ``complete=False`` and is never fed to
  training/backtests. A bucket is complete once ``bucket_start + tf <= now``
  (``now`` = current server time). In batch mode without ``now`` the latest
  bucket is treated as forming.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from services.market_data.provider import CanonicalCandle, CanonicalTick

# AI AURA target horizons in seconds.
TIMEFRAMES: tuple = (1, 3, 5, 10, 15, 30, 60, 180, 300, 900)


def bucket_start(timestamp: float, timeframe_s: int) -> int:
    """UTC epoch-second start of the bucket containing ``timestamp``."""
    if timeframe_s <= 0:
        raise ValueError("timeframe_s must be positive")
    return int(math.floor(timestamp / timeframe_s) * timeframe_s)


def _new_agg(start: int, price: float, ts: float) -> "_Agg":
    return _Agg(start, price, price, price, price, 1, ts, ts)


@dataclass
class _Agg:
    """Mutable accumulator for one bucket.

    open/close are keyed by tick timestamp, not arrival order, so ticks that
    arrive reordered WITHIN a bucket still yield open = earliest, close =
    latest — matching the batch path (which sorts first).
    """

    start: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    open_ts: float
    close_ts: float

    def update(self, price: float, ts: float) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        if ts < self.open_ts:
            self.open = price
            self.open_ts = ts
        if ts >= self.close_ts:
            self.close = price
            self.close_ts = ts
        self.tick_count += 1

    def to_candle(self, asset: str, timeframe_s: int, provider: str, complete: bool) -> CanonicalCandle:
        return CanonicalCandle(
            asset=asset,
            timeframe_s=timeframe_s,
            timestamp=float(self.start),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            tick_count=self.tick_count,
            volume=None,
            complete=complete,
            provider=provider,
        )


def build_candles(
    ticks: Iterable[CanonicalTick],
    timeframe_s: int,
    *,
    now: Optional[float] = None,
    include_forming: bool = False,
) -> List[CanonicalCandle]:
    """Aggregate ``ticks`` into candles for one timeframe (oldest first).

    Ticks are sorted by ``source_timestamp`` first (defensive; the raw stream
    is usually already ordered). Invalid prices (<=0 / non-finite) are skipped.
    Empty buckets produce no candle.

    Completeness: with ``now`` given, a candle is complete iff
    ``start + timeframe_s <= now``. Without ``now``, all but the most recent
    bucket are complete and the last is forming. ``include_forming`` controls
    whether the forming candle is returned at all.
    """
    valid = [t for t in ticks
             if isinstance(t.price, (int, float)) and math.isfinite(t.price) and t.price > 0]
    valid.sort(key=lambda t: t.source_timestamp)
    if not valid:
        return []

    asset = valid[0].asset
    provider = valid[0].provider

    aggs: List[_Agg] = []
    current: Optional[_Agg] = None
    for tick in valid:
        start = bucket_start(tick.source_timestamp, timeframe_s)
        if current is None or start != current.start:
            if current is not None:
                aggs.append(current)
            current = _new_agg(start, tick.price, tick.source_timestamp)
        else:
            current.update(tick.price, tick.source_timestamp)
    if current is not None:
        aggs.append(current)

    def is_complete(agg: _Agg, is_last: bool) -> bool:
        if now is not None:
            return (agg.start + timeframe_s) <= now
        return not is_last  # no clock: last bucket is forming

    candles: List[CanonicalCandle] = []
    last_index = len(aggs) - 1
    for i, agg in enumerate(aggs):
        complete = is_complete(agg, i == last_index)
        if not complete and not include_forming:
            continue
        candles.append(agg.to_candle(asset, timeframe_s, provider, complete))
    return candles


class CandleBuilder:
    """Incremental single-timeframe builder for the live stream.

    Feed ticks in (roughly) time order; completed candles are delivered to
    ``on_candle`` the moment a tick for a later bucket arrives. The forming
    candle is available via :meth:`forming`. Out-of-order ticks older than the
    current bucket are dropped (counted in ``dropped_out_of_order``) — the raw
    layer keeps them; the candle layer stays monotonic.
    """

    def __init__(
        self,
        asset: str,
        timeframe_s: int,
        on_candle: Optional[Callable[[CanonicalCandle], None]] = None,
        provider: str = "",
    ) -> None:
        if timeframe_s <= 0:
            raise ValueError("timeframe_s must be positive")
        self.asset = asset
        self.timeframe_s = timeframe_s
        self.provider = provider
        self._on_candle = on_candle
        self._current: Optional[_Agg] = None
        self.completed: List[CanonicalCandle] = []
        self.dropped_out_of_order = 0
        self.skipped_invalid = 0

    def add_tick(self, tick: CanonicalTick) -> Optional[CanonicalCandle]:
        """Add one tick. Returns a candle if this tick closed the previous one."""
        price = tick.price
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
            self.skipped_invalid += 1
            return None
        start = bucket_start(tick.source_timestamp, self.timeframe_s)
        emitted: Optional[CanonicalCandle] = None

        if self._current is None:
            self._current = _new_agg(start, price, tick.source_timestamp)
        elif start == self._current.start:
            self._current.update(price, tick.source_timestamp)
        elif start > self._current.start:
            emitted = self._current.to_candle(self.asset, self.timeframe_s, self.provider, complete=True)
            self.completed.append(emitted)
            if self._on_candle is not None:
                self._on_candle(emitted)
            self._current = _new_agg(start, price, tick.source_timestamp)
        else:  # start < current.start: stale tick for an already-closed bucket
            self.dropped_out_of_order += 1

        return emitted

    def flush(self, now: float) -> Optional[CanonicalCandle]:
        """Emit the forming candle as completed if its window has elapsed
        (``bucket_start + timeframe_s <= now``).

        Without this, a bucket is only closed when a later-bucket tick arrives,
        so on a quiet feed the final complete candle would never be emitted.
        Call periodically from the live loop (with server time).
        """
        if self._current is None:
            return None
        if (self._current.start + self.timeframe_s) <= now:
            candle = self._current.to_candle(self.asset, self.timeframe_s, self.provider, complete=True)
            self.completed.append(candle)
            if self._on_candle is not None:
                self._on_candle(candle)
            self._current = None
            return candle
        return None

    def forming(self) -> Optional[CanonicalCandle]:
        if self._current is None:
            return None
        return self._current.to_candle(self.asset, self.timeframe_s, self.provider, complete=False)


class MultiTimeframeCandleBuilder:
    """Fan one tick stream into several CandleBuilders (one per timeframe).

    Usable directly as a provider tick listener (single-asset). ``on_candle``
    receives every completed candle across all timeframes.
    """

    def __init__(
        self,
        asset: str,
        timeframes: Iterable[int] = TIMEFRAMES,
        on_candle: Optional[Callable[[CanonicalCandle], None]] = None,
        provider: str = "",
    ) -> None:
        self.asset = asset
        self._on_candle = on_candle
        self.builders: Dict[int, CandleBuilder] = {
            tf: CandleBuilder(asset, tf, on_candle=on_candle, provider=provider)
            for tf in timeframes
        }

    def __call__(self, tick: CanonicalTick) -> None:
        self.add_tick(tick)

    def add_tick(self, tick: CanonicalTick) -> None:
        if tick.asset != self.asset:
            return
        for builder in self.builders.values():
            builder.add_tick(tick)

    def flush(self, now: float) -> List[CanonicalCandle]:
        """Flush every timeframe's elapsed forming candle. Returns those emitted."""
        emitted: List[CanonicalCandle] = []
        for builder in self.builders.values():
            candle = builder.flush(now)
            if candle is not None:
                emitted.append(candle)
        return emitted

    def forming(self) -> Dict[int, CanonicalCandle]:
        out: Dict[int, CanonicalCandle] = {}
        for tf, builder in self.builders.items():
            candle = builder.forming()
            if candle is not None:
                out[tf] = candle
        return out

    def completed(self, timeframe_s: int) -> List[CanonicalCandle]:
        builder = self.builders.get(timeframe_s)
        return list(builder.completed) if builder else []
