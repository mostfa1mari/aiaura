"""Prediction-target / labeling tests (offline). No-look-ahead is the focus."""

import pytest

from services.labeling.target import (
    Direction,
    LabelConfig,
    ReferenceSeries,
    generate_labels,
    infer_price_precision,
    make_label,
)
from services.market_data.provider import SCHEMA_VERSION, CanonicalTick

BASE = 1787000000.0


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


def test_reference_price_is_last_at_or_before():
    s = ReferenceSeries([tk(BASE, 1.10), tk(BASE + 1, 1.11), tk(BASE + 2, 1.12)])
    assert s.price_at(BASE + 1.5, 2.0) == (1.11, "")   # last at/before 1.5 is @1s
    assert s.price_at(BASE + 1.0, 2.0) == (1.11, "")   # exact boundary -> that tick
    assert s.price_at(BASE + 2.0, 2.0) == (1.12, "")


def test_reference_before_first_and_after_last_are_invalid():
    s = ReferenceSeries([tk(BASE + 1, 1.10), tk(BASE + 2, 1.11)])
    price, reason = s.price_at(BASE, 2.0)
    assert price is None and "before first" in reason
    price, reason = s.price_at(BASE + 10, 2.0)
    assert price is None and "does not cover" in reason


def test_reference_staleness_guard():
    s = ReferenceSeries([tk(BASE, 1.10), tk(BASE + 100, 1.20)])
    # querying at t=50 (within covered range) but nearest prior tick is 50s old
    price, reason = s.price_at(BASE + 50, max_staleness_s=2.0)
    assert price is None and "stale" in reason


def test_label_up_down_flat():
    ticks = [tk(BASE + i, 1.10 + (i * 0.0) ) for i in range(0, 11)]
    # build a clear up move: entry @0 = 1.10, expiry @5 = 1.15
    ticks = [tk(BASE + i, p) for i, p in enumerate(
        [1.10, 1.10, 1.10, 1.10, 1.10, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15])]
    s = ReferenceSeries(ticks)
    up = make_label(s, BASE, LabelConfig(horizon_s=5))
    assert up.valid and up.direction is Direction.UP and up.buy_wins is True
    assert up.entry_price == 1.10 and up.expiry_price == 1.15

    # down move
    ticks_d = [tk(BASE + i, p) for i, p in enumerate(
        [1.20, 1.20, 1.20, 1.20, 1.20, 1.10, 1.10])]
    down = make_label(ReferenceSeries(ticks_d), BASE, LabelConfig(horizon_s=5))
    assert down.direction is Direction.DOWN and down.buy_wins is False

    # flat move
    flat = make_label(ReferenceSeries([tk(BASE + i, 1.10) for i in range(7)]),
                      BASE, LabelConfig(horizon_s=5))
    assert flat.direction is Direction.FLAT and flat.buy_wins is False


def test_entry_delay_shifts_reference():
    # price steps up at t=3; with entry_delay the entry reference moves past it
    ticks = [tk(BASE + i, p) for i, p in enumerate([1.10, 1.10, 1.10, 1.20, 1.20, 1.20, 1.20, 1.20])]
    s = ReferenceSeries(ticks)
    # entry@0 (no delay), horizon 2 -> entry 1.10 vs expiry@2 1.10 -> FLAT
    assert make_label(s, BASE, LabelConfig(horizon_s=2)).direction is Direction.FLAT
    # entry@0 with 3s delay -> effective entry@3 = 1.20, expiry@5 = 1.20 -> FLAT too,
    # but entry_price now reflects the delayed quote
    delayed = make_label(s, BASE, LabelConfig(horizon_s=2, entry_delay_s=3))
    assert delayed.entry_price == 1.20 and delayed.effective_entry_time == BASE + 3


def test_precision_rounding_creates_flat():
    ticks = [tk(BASE, 1.100001)] + [tk(BASE + i, 1.100004) for i in range(1, 7)]
    s = ReferenceSeries(ticks)
    # raw: 1.100004 > 1.100001 -> UP
    assert make_label(s, BASE, LabelConfig(horizon_s=5)).direction is Direction.UP
    # rounded to 5 decimals: both 1.10000 -> FLAT
    assert make_label(s, BASE, LabelConfig(horizon_s=5, price_precision=5)).direction is Direction.FLAT


def test_insufficient_data_excluded_not_guessed():
    s = ReferenceSeries([tk(BASE, 1.10), tk(BASE + 1, 1.11)])
    # expiry beyond data -> invalid, no direction
    lbl = make_label(s, BASE, LabelConfig(horizon_s=60))
    assert lbl.valid is False and lbl.direction is None and "expiry" in lbl.reason
    assert lbl.buy_wins is None


def test_no_lookahead_entry_price_ignores_future_moves():
    # A huge move happens AFTER the effective entry but before expiry; the entry
    # price must reflect only ticks <= effective entry.
    ticks = [tk(BASE, 1.10), tk(BASE + 1, 1.10), tk(BASE + 2, 9.99), tk(BASE + 5, 9.99)]
    s = ReferenceSeries(ticks)
    lbl = make_label(s, BASE, LabelConfig(horizon_s=5))
    assert lbl.entry_price == 1.10          # NOT 9.99 (that tick is in the future)
    assert lbl.expiry_price == 9.99
    assert lbl.direction is Direction.UP


def test_generate_labels_batch_and_series_reuse():
    ticks = [tk(BASE + i, 1.10 + i * 0.001) for i in range(20)]
    entries = [BASE + 0, BASE + 5, BASE + 10]
    labels = generate_labels(ticks, entries, LabelConfig(horizon_s=3))
    assert len(labels) == 3
    assert all(l.valid and l.direction is Direction.UP for l in labels)
    # passing a prebuilt series works too
    s = ReferenceSeries(ticks)
    labels2 = generate_labels(s, entries, LabelConfig(horizon_s=3))
    assert [l.direction for l in labels2] == [l.direction for l in labels]


def test_infer_price_precision():
    ticks = [tk(BASE, 1.10), tk(BASE + 1, 1.12345), tk(BASE + 2, 1.1)]
    assert infer_price_precision(ticks) == 5


def test_tied_timestamps_are_deterministic_regardless_of_input_order():
    # two ticks at the SAME source_timestamp, different arrival + price
    early = tk(BASE + 5, 1.20)                       # received earlier
    late = CanonicalTick(
        tick_id="EURUSD_otc:z", asset="EURUSD_otc", price=1.10,
        source_timestamp=BASE + 5, received_timestamp=early.received_timestamp + 0.5,
        latency_ms=0.0, provider="pocket_option", schema_version=SCHEMA_VERSION,
        raw_source_timestamp=BASE + 5 + 7200,
    )
    # the last-ARRIVING quote (late, 1.10) must win in BOTH input orderings
    for order in ([early, late], [late, early]):
        s = ReferenceSeries(order)
        price, _ = s.price_at(BASE + 5, 2.0)
        assert price == 1.10, f"non-deterministic tie for input order {order}"


def test_config_validation():
    with pytest.raises(ValueError):
        LabelConfig(horizon_s=0)
    with pytest.raises(ValueError):
        LabelConfig(horizon_s=5, entry_delay_s=-1)
