"""Canonical tick normalization tests (no network).

Verifies UTC normalization of server-native wire timestamps, latency
computation, buffering, and listener emission via _ingest_tick directly.
"""

from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.provider import SCHEMA_VERSION

FAKE_SSID = '42["auth",{"session":"testsession-not-real-0123456789","isDemo":1,"uid":1}]'
TZ_OFFSET = 7200.0  # server wire clock is UTC+2


class StubTimeSync:
    _stream_tz_offset = TZ_OFFSET

    def __init__(self, synced_now: float):
        self._now = synced_now

    def get_synced_time(self) -> float:
        return self._now


def make_provider() -> PocketOptionMarketDataProvider:
    return PocketOptionMarketDataProvider(FAKE_SSID, supervise=False)


def test_ingest_normalizes_wire_timestamp_to_utc():
    provider = make_provider()
    wire_ts = 1787000000.0 + TZ_OFFSET  # server-native
    sync = StubTimeSync(synced_now=1787000000.4)

    provider._ingest_tick("EURUSD_otc", wire_ts, 1.23456, sync)

    tick = provider.get_latest_tick("EURUSD_otc")
    assert tick is not None
    assert tick.source_timestamp == 1787000000.0
    assert tick.raw_source_timestamp == wire_ts
    assert abs(tick.latency_ms - 400.0) < 0.01  # float64 precision at epoch scale
    assert tick.provider == "pocket_option"
    assert tick.schema_version == SCHEMA_VERSION


def test_buffer_and_limits():
    provider = make_provider()
    sync = StubTimeSync(synced_now=1787000100.0)
    for i in range(250):
        provider._ingest_tick("EURUSD_otc", 1787000000.0 + i + TZ_OFFSET, 1.1 + i * 1e-5, sync)

    assert len(provider.get_realtime_ticks("EURUSD_otc", limit=100)) == 100
    assert len(provider.get_realtime_ticks("EURUSD_otc", limit=0)) == 250
    newest = provider.get_realtime_ticks("EURUSD_otc", limit=1)[0]
    assert newest.tick_id == provider.get_latest_tick("EURUSD_otc").tick_id
    assert provider.get_realtime_ticks("GBPUSD_otc") == []


def test_listeners_receive_canonical_ticks():
    provider = make_provider()
    sync = StubTimeSync(synced_now=1787000100.0)
    seen = []
    provider.add_tick_listener(seen.append)
    provider._ingest_tick("EURUSD_otc", 1787000000.0 + TZ_OFFSET, 1.5, sync)
    provider.remove_tick_listener(seen.append)
    provider._ingest_tick("EURUSD_otc", 1787000001.0 + TZ_OFFSET, 1.6, sync)

    assert len(seen) == 1
    assert seen[0].price == 1.5


def test_bad_listener_never_kills_the_feed():
    provider = make_provider()
    sync = StubTimeSync(synced_now=1787000100.0)

    def bad_listener(_tick):
        raise RuntimeError("boom")

    provider.add_tick_listener(bad_listener)
    provider._ingest_tick("EURUSD_otc", 1787000000.0 + TZ_OFFSET, 1.5, sync)
    assert provider.get_latest_tick("EURUSD_otc") is not None


def test_tick_ids_are_unique():
    provider = make_provider()
    sync = StubTimeSync(synced_now=1787000100.0)
    for i in range(100):
        provider._ingest_tick("EURUSD_otc", 1787000000.0 + TZ_OFFSET, 1.1, sync)
    ids = [t.tick_id for t in provider.get_realtime_ticks("EURUSD_otc", limit=0)]
    assert len(ids) == len(set(ids)) == 100


class _TzStub:
    """Minimal time_sync stub exposing _tz_detection_done."""

    def __init__(self, detected: bool):
        self._tz_detection_done = detected
        self._stream_tz_offset = TZ_OFFSET if detected else 0.0


class _ClientStub:
    def __init__(self, detected: bool):
        class _Api:
            pass

        self.api = _Api()
        self.api.time_sync = _TzStub(detected)


def test_history_gate_raises_when_tz_undetected_and_no_subscription():
    import pytest

    from services.market_data.provider import ProviderConnectionError

    provider = make_provider()
    provider._client = _ClientStub(detected=False)
    with pytest.raises(ProviderConnectionError):
        provider._ensure_stream_tz_detected(timeout_s=0.1)


def test_history_gate_passes_when_tz_detected():
    provider = make_provider()
    provider._client = _ClientStub(detected=True)
    # Should not raise.
    provider._ensure_stream_tz_detected(timeout_s=0.1)
