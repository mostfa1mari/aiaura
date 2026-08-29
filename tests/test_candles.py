"""Candle engine tests (offline)."""

from services.market_data.provider import SCHEMA_VERSION, CanonicalTick
from services.market_data.candles import (
    TIMEFRAMES,
    CandleBuilder,
    MultiTimeframeCandleBuilder,
    bucket_start,
    build_candles,
)

BASE = 1787000000  # divisible by all target timeframes? check below


def tk(ts: float, price: float, asset: str = "EURUSD_otc") -> CanonicalTick:
    return CanonicalTick(
        tick_id=f"{asset}:{int(ts * 1e9)}",
        asset=asset,
        price=price,
        source_timestamp=ts,
        received_timestamp=ts + 0.05,
        latency_ms=50.0,
        provider="pocket_option",
        schema_version=SCHEMA_VERSION,
        raw_source_timestamp=ts + 7200,
    )


def test_bucket_start():
    assert bucket_start(1787000003.7, 5) == 1787000000
    assert bucket_start(1787000005.0, 5) == 1787000005
    assert bucket_start(1787000009.9, 5) == 1787000005


def test_ohlc_within_one_bucket():
    # five ticks inside the same 5s bucket
    ticks = [
        tk(BASE + 0.0, 1.10),
        tk(BASE + 1.0, 1.12),  # high
        tk(BASE + 2.0, 1.08),  # low
        tk(BASE + 3.0, 1.11),
        tk(BASE + 4.0, 1.11),  # close
    ]
    # now well past this bucket so it is complete
    candles = build_candles(ticks, 5, now=BASE + 100)
    assert len(candles) == 1
    c = candles[0]
    assert c.timestamp == BASE
    assert c.open == 1.10
    assert c.high == 1.12
    assert c.low == 1.08
    assert c.close == 1.11
    assert c.tick_count == 5
    assert c.complete is True
    assert c.timeframe_s == 5


def test_multiple_buckets_and_gap_not_filled():
    # ticks in bucket 0 and bucket 2 (bucket 1 empty) -> only 2 candles, no fill
    ticks = [
        tk(BASE + 0, 1.10),
        tk(BASE + 1, 1.11),
        tk(BASE + 10, 1.20),  # bucket 2 (10..14)
        tk(BASE + 11, 1.21),
    ]
    candles = build_candles(ticks, 5, now=BASE + 100)
    assert [c.timestamp for c in candles] == [BASE, BASE + 10]
    assert all(c.complete for c in candles)


def test_forming_candle_excluded_by_default():
    ticks = [tk(BASE + 0, 1.10), tk(BASE + 6, 1.12)]
    # now inside the second bucket (5..9) -> forming
    candles = build_candles(ticks, 5, now=BASE + 7)
    assert [c.timestamp for c in candles] == [BASE]  # only the completed first bucket

    with_forming = build_candles(ticks, 5, now=BASE + 7, include_forming=True)
    assert [c.timestamp for c in with_forming] == [BASE, BASE + 5]
    assert with_forming[-1].complete is False


def test_no_now_last_bucket_is_forming():
    ticks = [tk(BASE + 0, 1.10), tk(BASE + 6, 1.12)]
    candles = build_candles(ticks, 5)  # no now
    assert [c.timestamp for c in candles] == [BASE]  # last bucket dropped as forming


def test_invalid_prices_skipped():
    ticks = [tk(BASE + 0, 1.10), tk(BASE + 1, 0.0), tk(BASE + 2, float("nan")), tk(BASE + 3, 1.15)]
    candles = build_candles(ticks, 5, now=BASE + 100)
    assert len(candles) == 1
    assert candles[0].tick_count == 2  # only the two valid ticks
    assert candles[0].close == 1.15


def test_unsorted_ticks_are_sorted():
    ticks = [tk(BASE + 4, 1.14), tk(BASE + 0, 1.10), tk(BASE + 2, 1.12)]
    candles = build_candles(ticks, 5, now=BASE + 100)
    assert candles[0].open == 1.10   # earliest by timestamp
    assert candles[0].close == 1.14  # latest by timestamp


def test_incremental_builder_emits_on_rollover():
    emitted = []
    builder = CandleBuilder("EURUSD_otc", 5, on_candle=emitted.append)
    assert builder.add_tick(tk(BASE + 0, 1.10)) is None
    assert builder.add_tick(tk(BASE + 1, 1.12)) is None
    # tick in next bucket closes the first candle
    candle = builder.add_tick(tk(BASE + 5, 1.20))
    assert candle is not None
    assert candle.timestamp == BASE
    assert candle.open == 1.10 and candle.high == 1.12 and candle.close == 1.12
    assert candle.complete is True
    assert len(emitted) == 1
    forming = builder.forming()
    assert forming.timestamp == BASE + 5 and forming.complete is False


def test_incremental_drops_stale_ticks():
    builder = CandleBuilder("EURUSD_otc", 5)
    builder.add_tick(tk(BASE + 6, 1.10))   # bucket 5..9
    builder.add_tick(tk(BASE + 1, 1.09))   # stale, belongs to closed bucket 0..4
    assert builder.dropped_out_of_order == 1
    assert builder.forming().timestamp == BASE + 5


def test_multi_timeframe_builder():
    seen = []
    mtf = MultiTimeframeCandleBuilder("EURUSD_otc", timeframes=(1, 5), on_candle=seen.append)
    for i in range(7):
        mtf(tk(BASE + i, 1.10 + i * 1e-4))  # __call__ as listener
    # 1s builder should have completed several candles; 5s at least one
    assert len(mtf.completed(1)) >= 5
    assert len(mtf.completed(5)) >= 1
    forming = mtf.forming()
    assert 1 in forming and 5 in forming


def test_multi_timeframe_ignores_other_asset():
    mtf = MultiTimeframeCandleBuilder("EURUSD_otc", timeframes=(1,))
    mtf.add_tick(tk(BASE, 1.10, asset="GBPUSD_otc"))
    assert mtf.forming() == {}


def test_target_timeframes_present():
    assert TIMEFRAMES == (1, 3, 5, 10, 15, 30, 60, 180, 300, 900)


def test_intra_bucket_reorder_matches_batch():
    # ticks arrive reordered WITHIN one 5s bucket
    arrival = [tk(BASE + 3, 1.15), tk(BASE + 0, 1.10), tk(BASE + 1, 1.12)]
    builder = CandleBuilder("EURUSD_otc", 5)
    for t in arrival:
        builder.add_tick(t)
    forming = builder.forming()
    # open = earliest ts (1.10), close = latest ts (1.15) — not arrival order
    assert forming.open == 1.10
    assert forming.close == 1.15
    assert forming.high == 1.15 and forming.low == 1.10
    assert builder.dropped_out_of_order == 0

    batch = build_candles(arrival, 5, now=BASE + 100, include_forming=True)
    assert batch[0].open == forming.open
    assert batch[0].close == forming.close


def test_flush_emits_terminal_complete_candle():
    emitted = []
    builder = CandleBuilder("EURUSD_otc", 5, on_candle=emitted.append)
    builder.add_tick(tk(BASE + 1, 1.10))
    # feed goes quiet; window elapses -> flush must emit the completed candle
    candle = builder.flush(now=BASE + 100)
    assert candle is not None
    assert candle.complete is True
    assert candle.timestamp == BASE
    assert len(emitted) == 1
    assert builder.forming() is None
    # parity with batch for the same tick
    batch = build_candles([tk(BASE + 1, 1.10)], 5, now=BASE + 100)
    assert batch[0].timestamp == candle.timestamp and batch[0].complete


def test_flush_noop_when_bucket_not_elapsed():
    builder = CandleBuilder("EURUSD_otc", 5)
    builder.add_tick(tk(BASE + 1, 1.10))
    assert builder.flush(now=BASE + 3) is None  # still within the bucket
    assert builder.forming() is not None


def test_multi_timeframe_flush():
    mtf = MultiTimeframeCandleBuilder("EURUSD_otc", timeframes=(1, 5))
    mtf.add_tick(tk(BASE + 1, 1.10))
    emitted = mtf.flush(now=BASE + 100)
    # both the 1s and 5s forming candles are now elapsed
    assert len(emitted) == 2
    assert all(c.complete for c in emitted)
