"""TickStore round-trip and partitioning tests (offline)."""

import pandas as pd
import pytest

from services.market_data.provider import SCHEMA_VERSION, CanonicalTick
from services.market_data.storage import TICK_COLUMNS, TickStore

DAY1 = 1787000000.0  # 2026-08-17 UTC (arbitrary fixed epoch inside a day)
DAY2 = DAY1 + 86400.0


def tick(i: int, ts: float, asset: str = "EURUSD_otc") -> CanonicalTick:
    return CanonicalTick(
        tick_id=f"{asset}:{int(ts * 1e9)}:{i}",
        asset=asset,
        price=1.1 + i * 0.0001,
        source_timestamp=ts,
        received_timestamp=ts + 0.05,
        latency_ms=50.0,
        provider="pocket_option",
        schema_version=SCHEMA_VERSION,
        raw_source_timestamp=ts + 7200,
    )


def test_roundtrip_schema_and_order(tmp_path):
    store = TickStore(tmp_path, flush_interval_s=10_000, flush_max_ticks=10_000)
    ticks = [tick(i, DAY1 + i) for i in range(50)]
    for t in reversed(ticks):  # append out of order; read must sort
        store.append(t)
    store.close()

    frame = store.read_day("EURUSD_otc", "2026-08-17")
    assert frame is not None
    assert list(frame.columns) == TICK_COLUMNS
    assert len(frame) == 50
    assert frame["source_timestamp"].is_monotonic_increasing
    assert store.ticks_persisted == 50
    assert set(frame["tick_id"]) == {t.tick_id for t in ticks}


def test_day_partitioning(tmp_path):
    store = TickStore(tmp_path, flush_interval_s=10_000, flush_max_ticks=10_000)
    store.append(tick(1, DAY1))
    store.append(tick(2, DAY2))
    store.close()

    assert (tmp_path / "EURUSD_otc" / "2026-08-17").exists()
    assert (tmp_path / "EURUSD_otc" / "2026-08-18").exists()
    assert len(store.read_day("EURUSD_otc", "2026-08-17")) == 1
    assert len(store.read_day("EURUSD_otc", "2026-08-18")) == 1


def test_flush_on_max_ticks(tmp_path):
    store = TickStore(tmp_path, flush_interval_s=10_000, flush_max_ticks=5)
    for i in range(5):
        store.append(tick(i, DAY1 + i))
    # buffer hit flush_max_ticks -> already on disk without close()
    assert store.ticks_persisted == 5


def test_read_missing_day_returns_none(tmp_path):
    store = TickStore(tmp_path)
    assert store.read_day("EURUSD_otc", "1999-01-01") is None


def test_partial_flush_failure_restores_all_unwritten_groups(tmp_path, monkeypatch):
    """If one group's write fails, that group AND all not-yet-written groups
    must be restored to the buffer — never silently dropped."""
    store = TickStore(tmp_path, flush_interval_s=10_000, flush_max_ticks=10_000)
    # Three groups: two assets across two days -> multiple (asset, day) groups.
    ticks = [
        tick(1, DAY1, "EURUSD_otc"),
        tick(2, DAY1, "GBPUSD_otc"),
        tick(3, DAY2, "AUDUSD_otc"),
    ]
    for t in ticks:
        store.append(t)

    real_to_parquet = pd.DataFrame.to_parquet
    calls = {"n": 0}

    def flaky_to_parquet(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk full on first group")
        return real_to_parquet(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", flaky_to_parquet)
    with pytest.raises(OSError):
        store.flush()

    # All three ticks must still be buffered (none written, none lost).
    monkeypatch.setattr(pd.DataFrame, "to_parquet", real_to_parquet)
    store.flush()
    total = 0
    for asset, day in [("EURUSD_otc", "2026-08-17"), ("GBPUSD_otc", "2026-08-17"), ("AUDUSD_otc", "2026-08-18")]:
        frame = store.read_day(asset, day)
        total += 0 if frame is None else len(frame)
    assert total == 3
    assert store.ticks_persisted == 3
