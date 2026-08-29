"""Raw tick persistence (Phase 3).

Layout: one directory per asset per UTC day, holding append-only Parquet
part files (Parquet cannot be appended in place, so each flush writes a new
part; a later compaction step can merge a finished day into a single file):

    data/raw/ticks/EURUSD_otc/2026-08-29/part-104512-3f2a.parquet

Partitioning is by the UTC date of ``source_timestamp``. Raw data is stored
exactly as received (no dedup, no gap fill) — quality handling is a separate,
derived layer (Phase 4).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from services.market_data.provider import CanonicalTick

logger = logging.getLogger(__name__)

TICK_COLUMNS = [
    "tick_id",
    "asset",
    "price",
    "source_timestamp",
    "received_timestamp",
    "latency_ms",
    "provider",
    "schema_version",
    "raw_source_timestamp",
]


def _utc_date(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


class TickStore:
    """Buffered, thread-safe Parquet writer for canonical ticks.

    ``append`` is cheap and safe to use directly as a provider tick listener.
    Flushes happen when the buffer reaches ``flush_max_ticks`` or on the first
    append after ``flush_interval_s`` elapsed, and always on ``close()``.
    """

    def __init__(
        self,
        root: Path | str = "data/raw/ticks",
        flush_interval_s: float = 10.0,
        flush_max_ticks: int = 2000,
    ) -> None:
        self.root = Path(root)
        self.flush_interval_s = flush_interval_s
        self.flush_max_ticks = flush_max_ticks
        self._buffer: List[CanonicalTick] = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.ticks_persisted = 0
        self.files_written: List[Path] = []

    # -- writing -------------------------------------------------------

    def append(self, tick: CanonicalTick) -> None:
        with self._lock:
            self._buffer.append(tick)
            if (
                len(self._buffer) >= self.flush_max_ticks
                or time.time() - self._last_flush >= self.flush_interval_s
            ):
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()

    def _flush_locked(self) -> None:
        self._last_flush = time.time()
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []

        groups: Dict[tuple, List[CanonicalTick]] = {}
        for tick in batch:
            key = (tick.asset, _utc_date(tick.source_timestamp))
            groups.setdefault(key, []).append(tick)

        ordered = list(groups.items())
        for i, ((asset, day), ticks) in enumerate(ordered):
            directory = self.root / asset / day
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            path = directory / f"part-{stamp}-{uuid.uuid4().hex[:8]}.parquet"
            frame = pd.DataFrame([asdict(t) for t in ticks], columns=TICK_COLUMNS)
            try:
                frame.to_parquet(path, index=False)
            except Exception:
                # Never lose raw data silently: restore the failing group AND
                # every group not yet attempted (not just the current one), in
                # original order, then surface the error.
                unwritten: List[CanonicalTick] = []
                for (_a, _d), remaining in ordered[i:]:
                    unwritten.extend(remaining)
                self._buffer = unwritten + self._buffer
                logger.exception("parquet flush failed for %s %s", asset, day)
                raise
            self.ticks_persisted += len(ticks)
            self.files_written.append(path)
            logger.debug("flushed %d ticks -> %s", len(ticks), path)

    # -- reading -------------------------------------------------------

    def read_day(self, asset: str, day: str) -> Optional[pd.DataFrame]:
        """All persisted ticks for one asset and UTC day (``YYYY-MM-DD``),
        sorted by source_timestamp. None when nothing exists."""
        directory = self.root / asset / day
        if not directory.exists():
            return None
        parts = sorted(directory.glob("*.parquet"))
        if not parts:
            return None
        frame = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
        return frame.sort_values("source_timestamp", ignore_index=True)
