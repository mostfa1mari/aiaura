"""Pocket Option OTC implementation of MarketDataProvider.

Read-only by design: this module exposes market data only and never
references the vendored library's order-execution paths (``buy``,
``check_win``, ``Buyv3``, ``openOrder``...). Enforced by
tests/test_no_order_execution.py.

Integration notes (see docs/POCKET_OPTION_API_AUDIT.md for the full audit):

* The vendored client sends WebSocket frames from ad-hoc event loops in the
  calling thread, which is unsafe with ``websockets``. We override
  ``api.send_websocket_request`` / ``api.send_subfor`` on OUR instance to
  route every send through the client's own I/O loop via
  ``asyncio.run_coroutine_threadsafe``.
* Tick capture is push-based: we tee ``api._on_stream_tick`` into our own
  canonical buffers (the library's ring buffer only keeps 500 ticks).
* Wire timestamps are server-native (typically UTC+2). Canonical timestamps
  are normalized to UTC using the library's detected stream offset.
* ``global_value`` in the vendored library is process-global, so exactly one
  connected provider instance is allowed per process.
* On a dropped socket the library reconnects and re-authenticates in-thread,
  but does NOT re-subscribe; our supervisor re-sends subscriptions on every
  disconnect→connect transition and rebuilds the client if its thread dies.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from pocketoptionapi.stable_api import PocketOption  # noqa: E402
import pocketoptionapi.global_value as global_value  # noqa: E402

from services.market_data.provider import (  # noqa: E402
    SCHEMA_VERSION,
    AssetInfo,
    AssetUnavailableError,
    CanonicalCandle,
    CanonicalTick,
    HealthStatus,
    MarketDataError,
    MarketDataProvider,
    ProviderConnectionError,
)

logger = logging.getLogger(__name__)

_HISTORY_OFFSET = 45000          # loadHistoryPeriod offset the server tolerates
_DISCONNECT_GRACE_S = 45.0       # let the vendored in-thread retries run first
_STALE_TICK_S = 15.0             # connected but silent this long -> re-subscribe
_RESUBSCRIBE_COOLDOWN_S = 20.0   # min gap between stale-triggered re-subscribes
_MAX_CONNECTION_EVENTS = 200


class PocketOptionMarketDataProvider(MarketDataProvider):
    name = "pocket_option"

    TICK_BUFFER_SIZE = 200_000

    _instance_connected = False  # vendored global state => one live instance/process
    _instance_lock = threading.Lock()

    def __init__(
        self,
        ssid: str,
        connect_timeout_s: float = 45.0,
        catalog_timeout_s: float = 15.0,
        supervise: bool = True,
    ) -> None:
        super().__init__()
        if not ssid:
            raise MarketDataError("ssid must be provided (see security.load_ssid)")
        self._ssid = ssid
        self._connect_timeout_s = connect_timeout_s
        self._catalog_timeout_s = catalog_timeout_s
        self._supervise = supervise

        self._client: Optional[PocketOption] = None
        self._lock = threading.RLock()      # guards fast in-memory state
        self._build_lock = threading.Lock()  # serializes connect/rebuild/disconnect
        self._buffers: Dict[str, Deque[CanonicalTick]] = {}
        self._last_tick: Dict[str, CanonicalTick] = {}
        self._subscribed: Dict[str, int] = {}          # asset -> period_s
        self._ticks_received = 0
        self._reconnect_count = 0
        self._connection_events: List[dict] = []
        self._terminal_reason: Optional[str] = None
        self._seq = itertools.count()

        self._stop = threading.Event()
        self._supervisor_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        # _build_lock (never self._lock) is held across the blocking connect so
        # it is mutually exclusive with _rebuild()/disconnect(); self._lock is
        # only taken for brief in-memory updates and never across blocking I/O.
        with self._build_lock:
            if self._client is not None and self.is_connected():
                return
            with PocketOptionMarketDataProvider._instance_lock:
                if PocketOptionMarketDataProvider._instance_connected:
                    raise ProviderConnectionError(
                        "another PocketOptionMarketDataProvider is already "
                        "connected in this process (vendored library uses "
                        "process-global state; one connection per process)"
                    )
                PocketOptionMarketDataProvider._instance_connected = True
            try:
                self._stop.clear()
                self._terminal_reason = None
                self._start_client()
            except Exception:
                with PocketOptionMarketDataProvider._instance_lock:
                    PocketOptionMarketDataProvider._instance_connected = False
                raise
            if self._supervise and self._supervisor_thread is None:
                self._supervisor_thread = threading.Thread(
                    target=self._supervisor_loop,
                    name="po-provider-supervisor",
                    daemon=True,
                )
                self._supervisor_thread.start()

    def _start_client(self) -> None:
        """Create a fresh PocketOption client, connect, wait for time sync
        and the asset catalog, then install our instance hooks."""
        self._record_event("connecting")
        global_value.websocket_is_connected = False
        global_value.check_websocket_if_error = False
        global_value.websocket_error_reason = None

        client = PocketOption(self._ssid)
        ok, err = client.connect()
        if not ok:
            self._record_event("connect_failed", str(err))
            raise ProviderConnectionError(f"Pocket Option connect failed: {err}")

        deadline = time.time() + self._connect_timeout_s
        while not (client.check_connect() and client.is_time_synced()):
            if self._stop.is_set():
                self._stop_client(client)
                raise ProviderConnectionError("aborted during connect (stop requested)")
            if time.time() > deadline:
                self._stop_client(client)
                raise ProviderConnectionError(
                    "connected but time sync not established within "
                    f"{self._connect_timeout_s}s"
                )
            if global_value.check_websocket_if_error:
                raise ProviderConnectionError(
                    f"connection error while syncing: {global_value.websocket_error_reason}"
                )
            time.sleep(0.05)

        self._install_instance_hooks(client)
        with self._lock:
            self._client = client
        self._record_event("connected")

        catalog_deadline = time.time() + self._catalog_timeout_s
        while not client.get_assets():
            if self._stop.is_set():
                break
            if time.time() > catalog_deadline:
                logger.warning("asset catalog not received within %.0fs", self._catalog_timeout_s)
                break
            time.sleep(0.1)

    def _install_instance_hooks(self, client: PocketOption) -> None:
        api = client.api
        io_loop = client._io_loop
        time_sync = api.time_sync

        def _send_threadsafe(name, msg, request_id="", no_force_send=True):
            data = f"42{json.dumps(msg)}"
            fut = asyncio.run_coroutine_threadsafe(
                api.websocket.send_message(data), io_loop
            )
            fut.result(timeout=10)

        def _send_subfor_threadsafe(asset: str):
            fut = asyncio.run_coroutine_threadsafe(
                api.websocket.send_subfor(asset), io_loop
            )
            fut.result(timeout=10)

        api.send_websocket_request = _send_threadsafe
        api.send_subfor = _send_subfor_threadsafe

        original_on_tick = api._on_stream_tick

        def _tee(asset, timestamp, price):
            original_on_tick(asset, timestamp, price)
            try:
                self._ingest_tick(asset, timestamp, price, time_sync)
            except Exception:
                logger.exception("tick ingestion failed")

        api._on_stream_tick = _tee

    def disconnect(self) -> None:
        self._stop.set()
        supervisor = self._supervisor_thread
        if supervisor is not None:
            supervisor.join(timeout=60)  # long enough to outlast a stuck rebuild
            self._supervisor_thread = None
        # Hold _build_lock so a supervisor rebuild in flight completes (or is
        # aborted by _stop) before we tear down — otherwise it could assign a
        # fresh live client after we've torn the old one down.
        with self._build_lock:
            with self._lock:
                client = self._client
                self._client = None
            # _stop_client blocks on the io loop; call it WITHOUT self._lock so
            # a concurrent _ingest_tick on that loop can take the lock and the
            # close can complete (no cross-thread lock/loop deadlock).
            if client is not None:
                self._stop_client(client)
            self._record_event("disconnected_by_user")
        with PocketOptionMarketDataProvider._instance_lock:
            PocketOptionMarketDataProvider._instance_connected = False

    @staticmethod
    def _stop_client(client: PocketOption) -> None:
        """Permanently stop a vendored client.

        Closing the socket alone is not enough: the vendored ``connect()`` loop
        immediately reconnects to the next server. Maxing out
        ``reconnect_attempts`` first makes that loop exit (and its I/O thread
        wind down) after the socket closes, so the connection stays down.
        """
        try:
            ws = client.api.websocket
            ws.reconnect_attempts = ws.max_reconnect_attempts
        except Exception:
            logger.debug("could not cap reconnect_attempts", exc_info=True)
        try:
            client.disconnect_websocket()
        except Exception:
            logger.debug("disconnect_websocket failed", exc_info=True)

    def is_connected(self) -> bool:
        return (
            self._client is not None
            and not self._stop.is_set()
            and bool(global_value.websocket_is_connected)
        )

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def get_assets(self) -> Dict[str, AssetInfo]:
        client = self._require_client()
        catalog: Dict[str, AssetInfo] = {}
        for symbol, row in client.get_assets().items():
            timeframes = row.get("timeframes") or ()
            try:
                timeframes = tuple(int(t) for t in timeframes)
            except (TypeError, ValueError):
                timeframes = ()
            catalog[symbol] = AssetInfo(
                symbol=symbol,
                name=str(row.get("name", symbol)),
                category=str(row.get("category", "unknown")),
                payout=row.get("payout"),
                is_available=bool(row.get("is_available", False)),
                timeframes=timeframes,
            )
        return catalog

    def get_otc_assets(self) -> Dict[str, AssetInfo]:
        """Available OTC symbols only (AI AURA's target market)."""
        return {
            symbol: info
            for symbol, info in self.get_assets().items()
            if info.is_otc and info.is_available
        }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def subscribe(self, asset: str, period_s: int = 1) -> None:
        client = self._require_client()
        catalog = self.get_assets()
        if catalog:
            info = catalog.get(asset)
            if info is None:
                otc_sample = ", ".join(sorted(k for k in catalog if k.endswith("_otc"))[:8])
                raise AssetUnavailableError(
                    f"asset {asset!r} not in the live catalog "
                    f"({len(catalog)} assets; OTC examples: {otc_sample})"
                )
            if not info.is_available:
                raise AssetUnavailableError(
                    f"asset {asset!r} exists but is_available=False right now"
                )
        else:
            logger.warning("subscribing to %s without a loaded catalog", asset)

        if not client.subscribe(asset, period_s):
            raise MarketDataError(f"subscribe({asset!r}) failed")
        with self._lock:
            self._subscribed[asset] = period_s
        self._record_event("subscribed", asset)

    def unsubscribe(self, asset: str) -> None:
        """Best effort: the vendored client has no unsubscribe channel, so we
        send the platform's ``unsubfor`` frame directly."""
        client = self._require_client()
        with self._lock:
            self._subscribed.pop(asset, None)
        try:
            io_loop = client._io_loop
            fut = asyncio.run_coroutine_threadsafe(
                client.api.websocket.send_message(f"42{json.dumps(['unsubfor', asset])}"),
                io_loop,
            )
            fut.result(timeout=10)
            self._record_event("unsubscribed", asset)
        except Exception as exc:
            logger.warning("unsubscribe(%s) best-effort send failed: %s", asset, exc)

    def wait_for_first_tick(self, asset: str, timeout_s: float = 10.0) -> bool:
        """Block until a tick for ``asset`` has been received. Returns success.

        Use after ``subscribe`` in a warm-up flow so the stream timezone offset
        is established before requesting historical candles.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.get_latest_tick(asset) is not None:
                return True
            time.sleep(0.05)
        return self.get_latest_tick(asset) is not None

    def _ensure_stream_tz_detected(self, timeout_s: float = 8.0) -> None:
        """Ensure the stream tz offset has been detected (needs a live tick).

        Raises ProviderConnectionError if it cannot be established, rather than
        letting history be silently misaligned by a default offset of 0.
        """
        time_sync = self._require_client().api.time_sync
        if getattr(time_sync, "_tz_detection_done", False):
            return
        if not self._has_subscriptions():
            raise ProviderConnectionError(
                "stream timezone offset not yet established: subscribe to an "
                "asset and receive at least one tick (see wait_for_first_tick) "
                "before requesting historical candles, otherwise candle "
                "timestamps would be misaligned from the live stream."
            )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if getattr(time_sync, "_tz_detection_done", False):
                return
            time.sleep(0.05)
        raise ProviderConnectionError(
            f"stream timezone offset not detected within {timeout_s}s despite "
            "an active subscription (no ticks received?)"
        )

    def get_latest_tick(self, asset: str) -> Optional[CanonicalTick]:
        with self._lock:
            return self._last_tick.get(asset)

    def get_realtime_ticks(self, asset: str, limit: int = 100) -> List[CanonicalTick]:
        with self._lock:
            buffer = self._buffers.get(asset)
            if not buffer:
                return []
            ticks = list(buffer)
        return ticks[-limit:] if limit and limit > 0 else ticks

    def _ingest_tick(self, asset, wire_timestamp, price, time_sync) -> None:
        received = time.time()
        tz_offset = float(getattr(time_sync, "_stream_tz_offset", 0.0) or 0.0)
        source_utc = float(wire_timestamp) - tz_offset
        synced_now = time_sync.get_synced_time()
        latency_ms = (synced_now - source_utc) * 1000.0

        tick = CanonicalTick(
            tick_id=f"{asset}:{int(received * 1e9)}:{next(self._seq)}",
            asset=str(asset),
            price=float(price),
            source_timestamp=source_utc,
            received_timestamp=received,
            latency_ms=latency_ms,
            provider=self.name,
            schema_version=SCHEMA_VERSION,
            raw_source_timestamp=float(wire_timestamp),
        )
        with self._lock:
            buffer = self._buffers.get(asset)
            if buffer is None:
                buffer = deque(maxlen=self.TICK_BUFFER_SIZE)
                self._buffers[asset] = buffer
            buffer.append(tick)
            self._last_tick[asset] = tick
            self._ticks_received += 1
        self._emit_tick(tick)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_historical_candles(
        self,
        asset: str,
        timeframe_s: int,
        end_time: Optional[float] = None,
        pages: int = 1,
    ) -> List[CanonicalCandle]:
        client = self._require_client()
        time_sync = client.api.time_sync
        # The stream timezone offset is only detected once the first live tick
        # arrives. Serving history before then would use tz_offset=0 and return
        # candle timestamps ~2h off from the live stream, silently. Require
        # detection first (a subscribed stream provides it within seconds).
        self._ensure_stream_tz_detected()
        tz_offset = float(getattr(time_sync, "_stream_tz_offset", 0.0) or 0.0)

        start_time = None
        if end_time is not None:
            start_time = int(end_time + tz_offset)  # canonical UTC -> server-native

        raw = client.get_historical_candles(
            asset,
            timeframe_s,
            start_time=start_time,
            offset=_HISTORY_OFFSET,
            count_request=max(1, pages),
        )
        if not raw:
            return []

        candles: List[CanonicalCandle] = []
        for row in raw:
            try:
                volume = row.get("volume")
                candles.append(
                    CanonicalCandle(
                        asset=asset,
                        timeframe_s=timeframe_s,
                        timestamp=float(row["time"]) - tz_offset,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        tick_count=int(volume) if volume is not None else None,
                        volume=float(volume) if volume is not None else None,
                        complete=True,  # the client strips the forming candle
                        provider=self.name,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed candle row %r: %s", row, exc)
        candles.sort(key=lambda c: c.timestamp)
        return candles

    # ------------------------------------------------------------------
    # Health / supervision
    # ------------------------------------------------------------------

    def health_check(self) -> HealthStatus:
        with self._lock:
            last = max(
                self._last_tick.values(),
                key=lambda t: t.received_timestamp,
                default=None,
            )
            last_at = last.received_timestamp if last else None
            status = HealthStatus(
                connected=self.is_connected(),
                time_synced=bool(self._client and self._client.is_time_synced()),
                subscribed_assets=tuple(self._subscribed),
                ticks_received=self._ticks_received,
                last_tick_at=last_at,
                last_tick_age_s=(time.time() - last_at) if last_at else None,
                reconnect_count=self._reconnect_count,
                connection_events=list(self._connection_events[-20:]),
                detail=self._terminal_reason
                or str(global_value.websocket_error_reason or ""),
            )
        return status

    def force_disconnect_for_test(self) -> bool:
        """Close the live socket to exercise reconnection (validation only)."""
        client = self._require_client()
        self._record_event("forced_disconnect_test")
        return client.disconnect_websocket()

    def _supervisor_loop(self) -> None:
        was_connected = True
        disconnected_since: Optional[float] = None
        last_resubscribe = 0.0
        rebuild_backoff = 0
        while not self._stop.wait(1.0):
            connected = bool(global_value.websocket_is_connected)
            error_reason = str(global_value.websocket_error_reason or "")

            if global_value.check_websocket_if_error and "Unauthorized" in error_reason:
                if self._terminal_reason is None:
                    self._terminal_reason = error_reason
                    self._record_event("auth_failed_terminal", error_reason)
                    logger.error(
                        "Pocket Option rejected the session (SSID likely "
                        "expired). Capture a fresh PO_SSID into .env and "
                        "restart. Not retrying."
                    )
                continue

            if connected:
                rebuild_backoff = 0
                disconnected_since = None
                if not was_connected:
                    # Reconnected in-thread; server-side subscriptions died with
                    # the old socket — re-send them.
                    self._record_event("reconnected_in_thread")
                    self._reconnect_count += 1
                    if self._resubscribe_all():
                        last_resubscribe = time.time()
                else:
                    # Safety net for reconnects too fast to catch as a
                    # transition, or any silent server-side subscription loss:
                    # if we hold subscriptions but ticks have gone quiet, and
                    # the cooldown has elapsed, re-subscribe.
                    now = time.time()
                    if (
                        self._has_subscriptions()
                        and self._seconds_since_last_tick() > _STALE_TICK_S
                        and now - last_resubscribe > _RESUBSCRIBE_COOLDOWN_S
                    ):
                        self._record_event("stale_resubscribe", f"no ticks {_STALE_TICK_S:.0f}s+")
                        if self._resubscribe_all():
                            last_resubscribe = now
            else:
                if was_connected:
                    self._record_event("disconnect_detected")
                    disconnected_since = time.time()
                # Only rebuild once the vendored I/O thread has actually died.
                # While it is alive the library owns reconnection; spawning a
                # fresh client in parallel could yield two live sockets feeding
                # the shared global state.
                thread_alive = self._client_thread_alive()
                waited = time.time() - disconnected_since if disconnected_since else 0.0
                if not thread_alive and waited >= _DISCONNECT_GRACE_S:
                    delay = min(60, 5 * (2 ** rebuild_backoff))
                    self._record_event("rebuilding_client", f"thread dead after {waited:.0f}s, backoff {delay}s")
                    if self._stop.wait(delay):
                        break
                    try:
                        self._rebuild()
                        disconnected_since = None
                        rebuild_backoff = 0
                        last_resubscribe = time.time()
                    except Exception as exc:
                        rebuild_backoff = min(rebuild_backoff + 1, 4)
                        self._record_event("rebuild_failed", str(exc))
                        logger.warning("provider rebuild failed: %s", exc)

            was_connected = connected

    def _rebuild(self) -> None:
        # Mutually exclusive with connect()/disconnect() via _build_lock.
        with self._build_lock:
            if self._stop.is_set():
                return
            with self._lock:
                old = self._client
                self._client = None
            if old is not None:
                self._stop_client(old)
            self._reconnect_count += 1
            self._start_client()
            self._resubscribe_all()
            self._record_event("rebuilt")

    def _client_thread_alive(self) -> bool:
        client = self._client
        thread = getattr(client, "websocket_thread", None) if client else None
        return bool(thread is not None and thread.is_alive())

    def _has_subscriptions(self) -> bool:
        with self._lock:
            return bool(self._subscribed)

    def _seconds_since_last_tick(self) -> float:
        with self._lock:
            last = max(
                (t.received_timestamp for t in self._last_tick.values()),
                default=None,
            )
        return (time.time() - last) if last is not None else float("inf")

    def _resubscribe_all(self) -> bool:
        client = self._client
        if client is None:
            return False
        with self._lock:
            subscriptions = dict(self._subscribed)
        any_ok = False
        for asset, period_s in subscriptions.items():
            try:
                if client.subscribe(asset, period_s):
                    self._record_event("resubscribed", asset)
                    any_ok = True
                else:
                    self._record_event("resubscribe_failed", asset)
            except Exception as exc:
                self._record_event("resubscribe_failed", f"{asset}: {exc}")
        return any_ok

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_client(self) -> PocketOption:
        client = self._client
        if client is None:
            raise ProviderConnectionError("provider is not connected (call connect())")
        return client

    def _record_event(self, event: str, detail: str = "") -> None:
        entry = {"ts": time.time(), "event": event, "detail": detail}
        with self._lock:
            self._connection_events.append(entry)
            if len(self._connection_events) > _MAX_CONNECTION_EVENTS:
                del self._connection_events[: -_MAX_CONNECTION_EVENTS]
        logger.info("connection event: %s %s", event, detail)

    @property
    def connection_events(self) -> List[dict]:
        with self._lock:
            return list(self._connection_events)
