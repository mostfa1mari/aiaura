"""Data-quality layer tests (offline)."""

import math

from services.market_data.provider import SCHEMA_VERSION, CanonicalTick
from services.market_data.quality import (
    QualityReport,
    TickQualityMonitor,
    analyze_ticks,
    summarize_connection_events,
)

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


def test_clean_stream_is_good():
    ticks = [tk(BASE + i, 1.1000 + i * 1e-5) for i in range(60)]
    report = analyze_ticks(ticks)
    assert report.tick_count == 60
    assert report.is_clean
    assert report.quality_grade == "GOOD"
    assert report.out_of_order == 0
    assert report.gaps == 0
    assert abs(report.mean_interval_s - 1.0) < 1e-9


def test_out_of_order_detected():
    ticks = [tk(BASE, 1.1), tk(BASE + 2, 1.1), tk(BASE + 1, 1.1)]
    report = analyze_ticks(ticks)
    assert report.out_of_order == 1
    assert not report.is_clean


def test_gap_and_abnormal_gap():
    ticks = [tk(BASE, 1.1), tk(BASE + 8, 1.1), tk(BASE + 8 + 45, 1.1)]
    report = analyze_ticks(ticks, gap_s=5.0, abnormal_gap_s=30.0)
    assert report.gaps == 2
    assert report.abnormal_gaps == 1
    assert report.max_gap_s == 45.0


def test_invalid_price_detected():
    ticks = [tk(BASE, 1.1), tk(BASE + 1, 0.0), tk(BASE + 2, -3.0), tk(BASE + 3, float("nan"))]
    report = analyze_ticks(ticks)
    assert report.invalid_prices == 3
    assert not report.is_clean


def test_future_timestamp_detected():
    now = BASE + 5
    ticks = [tk(BASE + 1, 1.1), tk(BASE + 100, 1.1)]
    report = analyze_ticks(ticks, now=now)
    assert report.future_timestamps == 1


def test_price_spike_detected():
    ticks = [tk(BASE, 1.1000), tk(BASE + 1, 1.1000), tk(BASE + 2, 1.2000)]
    report = analyze_ticks(ticks, spike_pct=0.005)
    assert report.price_spikes == 1


def test_duplicate_detected():
    ticks = [tk(BASE, 1.1), tk(BASE, 1.1)]  # identical ts and price
    report = analyze_ticks(ticks)
    assert report.duplicates == 1


def test_empty_report():
    report = analyze_ticks([])
    assert report.tick_count == 0
    assert report.quality_grade == "POOR"


def test_monitor_matches_batch_counts():
    ticks = [tk(BASE, 1.1), tk(BASE + 8, 1.1), tk(BASE + 2, 1.1), tk(BASE + 3, 0.0)]
    monitor = TickQualityMonitor(gap_s=5.0)
    for t in ticks:
        monitor.observe(t)
    report = monitor.report_for("EURUSD_otc")
    assert isinstance(report, QualityReport)
    assert report.tick_count == 4
    assert report.gaps == 1          # BASE -> BASE+8
    assert report.out_of_order == 1  # BASE+8 -> BASE+2
    assert report.invalid_prices == 1


def test_monitor_usable_as_listener():
    monitor = TickQualityMonitor()
    monitor(tk(BASE, 1.1))  # __call__
    assert monitor.report_for("EURUSD_otc").tick_count == 1


def test_error_count_from_exact_counters_not_truncated_issues():
    # one invalid price (error) followed by many gap warnings that overflow the
    # monitor's recent-issue buffer; the error must still be counted.
    monitor = TickQualityMonitor(gap_s=5.0, recent_issue_limit=50)
    monitor.observe(tk(BASE, 0.0))  # invalid -> 1 error counter
    for i in range(1, 60):
        monitor.observe(tk(BASE + i * 8, 1.10 + i * 1e-5))  # 8s gaps -> warnings
    report = monitor.report_for("EURUSD_otc")
    assert report.invalid_prices == 1
    assert report.error_count >= 1     # exact counter, not the truncated list
    assert report.is_clean is False
    assert report.quality_grade != "GOOD"
    assert len(report.issues) <= 50    # display buffer still bounded


def test_monitor_matches_batch_after_invalid_price():
    # a valid tick FOLLOWING an invalid one must be handled identically
    ticks = [tk(BASE, 1.10), tk(BASE + 1, 0.0), tk(BASE + 10, 1.10)]
    batch = analyze_ticks(ticks, gap_s=5.0)
    monitor = TickQualityMonitor(gap_s=5.0)
    for t in ticks:
        monitor.observe(t)
    live = monitor.report_for("EURUSD_otc")
    assert batch.gaps == live.gaps == 1           # gap across the invalid tick
    assert batch.invalid_prices == live.invalid_prices == 1
    assert batch.out_of_order == live.out_of_order

    # spike straddling an invalid tick
    ticks2 = [tk(BASE, 1.10), tk(BASE + 1, 0.0), tk(BASE + 2, 1.20)]
    batch2 = analyze_ticks(ticks2, spike_pct=0.005)
    monitor2 = TickQualityMonitor(spike_pct=0.005)
    for t in ticks2:
        monitor2.observe(t)
    assert batch2.price_spikes == monitor2.report_for("EURUSD_otc").price_spikes == 1


def test_monitor_computes_mean_interval():
    monitor = TickQualityMonitor()
    for i in range(10):
        monitor.observe(tk(BASE + i, 1.10))  # 1s apart
    report = monitor.report_for("EURUSD_otc")
    assert abs(report.mean_interval_s - 1.0) < 1e-9
    # and it matches the batch for the same ticks
    batch = analyze_ticks([tk(BASE + i, 1.10) for i in range(10)])
    assert abs(batch.mean_interval_s - report.mean_interval_s) < 1e-9


def test_summarize_connection_events():
    events = [
        {"event": "disconnect_detected"},
        {"event": "reconnected_in_thread"},
        {"event": "resubscribed"},
        {"event": "rebuilt"},
        {"event": "auth_failed_terminal"},
    ]
    summary = summarize_connection_events(events)
    assert summary["interruptions"] == 1
    assert summary["reconnects"] == 2
    assert summary["resubscribes"] == 1
    assert summary["auth_failures"] == 1
