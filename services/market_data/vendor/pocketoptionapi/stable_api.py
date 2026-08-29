"""
PocketOption API Client — stable public interface.

Usage:
    api = PocketOption(ssid)
    ok, err = api.connect()
    candles = api.get_historical_candles("EURUSD_otc", 60, offset=9000)

Public methods live on :class:`PocketOption`. They use a WebSocket client
(``loadHistoryPeriod``, ``openOrder``, ``updateStream``, etc.) and optional
reconciliation of candles with ``updateHistoryNewFast`` / stream ticks; see
each method’s docstring.
"""

import asyncio
import threading
import time
import logging
import operator
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, List, Union, Tuple, Callable
from tzlocal import get_localzone
from queue import Queue

import pandas as pd

from pocketoptionapi.api import PocketOptionAPI
import pocketoptionapi.constants as OP_code
import pocketoptionapi.global_value as global_value

logger = logging.getLogger(__name__)

LOCAL_TIMEZONE = get_localzone()


class PocketOption:
    """High-level client for the PocketOption demo/live WebSocket API.

    Typical flow: :meth:`connect` → :meth:`subscribe` (for ticks / history
    helpers) → :meth:`get_historical_candles` or :meth:`get_realtime_ticks`.

    Time sync uses the HTTP ``Date`` header on connect and refines offset with
    ``updateStream`` ticks; :meth:`get_historical_candles` requires
    :meth:`is_time_synced` to be true (or it returns ``None``).
    """

    __version__ = "2.0.0"

    CANDLE_SIZES = [
        1, 5, 10, 15, 30, 60, 120, 300, 600, 900,
        1800, 3600, 7200, 14400, 28800, 43200, 86400,
        604800, 2592000,
    ]

    def __init__(self, ssid: str):
        """
        Args:
            ssid: Socket.IO auth payload string (e.g. ``42["auth",{...}]``)
                stored in ``global_value.SSID`` for the WebSocket handshake.
        """
        global_value.SSID = ssid
        self.api = PocketOptionAPI()
        # Set in :meth:`_run_websocket_thread` for cross-thread close (e.g. backend disconnect).
        self._io_loop: Optional[asyncio.AbstractEventLoop] = None

        self._check_win_queue = Queue()
        self._check_win_thread = threading.Thread(target=self._check_win_worker, daemon=True)
        self._check_win_thread.start()

    # ==================================================================
    # Connection
    # ==================================================================

    def connect(self) -> Tuple[bool, Optional[str]]:
        """Open the WebSocket in a background thread and wait for connection.

        Polls ``global_value.websocket_is_connected`` and error flags for up to
        30 seconds.

        Returns:
            ``(True, None)`` on success, or ``(False, message)`` on timeout or
            connection error (see ``global_value.websocket_error_reason``).
        """
        try:
            self.websocket_thread = threading.Thread(
                target=self._run_websocket_thread, daemon=True
            )
            self.websocket_thread.start()

            deadline = time.time() + 30
            while time.time() < deadline:
                if global_value.check_websocket_if_error:
                    return False, global_value.websocket_error_reason
                if global_value.websocket_is_connected:
                    return True, None
                time.sleep(0.1)

            return False, "Connection timeout"
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False, str(e)

    def disconnect_websocket(self, timeout: float = 12.0) -> bool:
        """Close the active WebSocket from another thread (shared I/O loop).

        Used when the process must drop the session before opening a new SSID.

        Returns:
            True if a close was scheduled on a running loop, False otherwise.
        """
        loop = getattr(self, "_io_loop", None)
        if loop is None or not loop.is_running():
            return False

        async def _close_ws() -> None:
            try:
                wc = self.api.websocket
                if wc.websocket is not None:
                    await wc.websocket.close()
            except Exception as exc:
                logger.debug("disconnect_websocket: %s", exc)

        fut = asyncio.run_coroutine_threadsafe(_close_ws(), loop)
        try:
            fut.result(timeout=timeout)
        except Exception as exc:
            logger.debug("disconnect_websocket result: %s", exc)
        return True

    def _run_websocket_thread(self):
        loop = asyncio.new_event_loop()
        self._io_loop = loop
        try:
            asyncio.set_event_loop(loop)

            global_value.websocket_is_connected = False
            global_value.check_websocket_if_error = False
            global_value.websocket_error_reason = None
            global_value.ssl_Mutual_exclusion = False
            global_value.ssl_Mutual_exclusion_write = False

            loop.run_until_complete(self.api.websocket.connect())
            loop.run_forever()
        except Exception as e:
            logger.error(f"WebSocket thread error: {e}")
            global_value.check_websocket_if_error = True
            global_value.websocket_error_reason = str(e)
        finally:
            loop.close()

    @staticmethod
    def check_connect() -> bool:
        """Return whether the WebSocket is marked connected (``global_value``)."""
        return global_value.websocket_is_connected

    def is_time_synced(self) -> bool:
        """True if :class:`~pocketoptionapi.ws.objects.time_sync.TimeSynchronizer`
        has server/local references and offset set (same condition as
        ``time_sync.is_synced()``). Required before :meth:`get_historical_candles`."""
        ts = self.api.time_sync
        return (ts.server_time_reference is not None
                and ts.local_time_reference is not None
                and ts.offset is not None)

    # ==================================================================
    # Time
    # ==================================================================

    def get_server_timestamp(self) -> float:
        """Estimated server time as Unix seconds (UTC), via ``TimeSynchronizer.get_synced_time``.

        If time is not synced, falls back to local ``time.time()`` (see implementation).
        """
        return self.api.time_sync.get_synced_time()

    def get_server_datetime(self) -> datetime:
        """``get_synced_time()`` as a timezone-aware :class:`datetime` (local zone from ``tzlocal``)."""
        return self.api.time_sync.get_synced_datetime()

    # ==================================================================
    # Balance
    # ==================================================================

    def get_balance(self) -> Optional[float]:
        """Last balance from the server if ``global_value.balance_updated`` is set; else ``None``."""
        if global_value.balance_updated:
            return global_value.balance
        return None

    # ==================================================================
    # Assets / Payout / Sentiment
    # ==================================================================

    def get_assets(self) -> Dict[str, dict]:
        """Copy of the asset catalog built from ``updateAssets`` (see ``_handle_assets_update``).

        Each symbol maps to a dict with keys: ``id``, ``name``, ``category``,
        ``payout``, ``is_available``, ``timeframes``, ``raw`` (full row list).
        """
        return dict(self.api._assets_catalog)

    def get_payout(self, asset: str) -> Optional[int]:
        """Payout percentage for ``asset`` from ``_assets_catalog``, or ``None`` if unknown."""
        info = self.api._assets_catalog.get(asset)
        return info["payout"] if info else None

    def get_sentiment(self, asset: str) -> Optional[float]:
        """Sentiment value for ``asset`` from the ``chafor`` message cache, or ``None``."""
        return self.api._sentiment.get(asset)

    # ==================================================================
    # Deals / Orders
    # ==================================================================

    def get_open_deals(self) -> list:
        """Shallow copy of open deals from ``updateOpenedDeals`` (list of server payloads)."""
        return list(self.api._opened_deals)

    def get_closed_deals(self) -> list:
        """Shallow copy of closed deals from ``updateClosedDeals``."""
        return list(self.api._closed_deals)

    def get_pending_deals(self) -> list:
        """Shallow copy of pending deals last set by ``successupdatePending``."""
        return list(self.api._pending_deals)

    # ==================================================================
    # Subscribe / Unsubscribe (real-time stream)
    # ==================================================================

    def subscribe(self, asset: str, period: int = 60) -> bool:
        """Subscribe to chart/stream data for an asset.

        Sends ``changeSymbol`` with the given period, then ``subfor`` so the
        server emits ``updateStream`` / ``updateHistoryNewFast`` for that
        symbol. Sets ``api.current_asset`` and ``api.current_period``.

        Args:
            asset: Symbol string (e.g. ``\"EURUSD_otc\"``).
            period: Candle period in seconds (must match server-supported
                values; see ``CANDLE_SIZES``).

        Returns:
            ``True`` if the calls succeeded, ``False`` if not connected or on error.
        """
        if not self.check_connect():
            logger.error("Not connected")
            return False
        try:
            self.api.change_symbol(asset, period)
            self.api.send_subfor(asset)
            self.api.current_asset = asset
            self.api.current_period = period
            logger.info(f"Subscribed to {asset} period={period}")
            return True
        except Exception as e:
            logger.error(f"Subscribe error: {e}")
            return False

    def get_realtime_ticks(self, asset: Optional[str] = None, limit: int = 100) -> List[tuple]:
        """Recent ``updateStream`` ticks from the in-memory ring buffer (per asset).

        Args:
            asset: Symbol to read; defaults to ``api.current_asset`` (set by
                :meth:`subscribe`). If both are missing, returns an empty list.
            limit: Maximum number of ticks; only the last ``limit`` entries are
                returned (see ``PocketOptionAPI.get_stream_ticks``).

        Returns:
            List of ``(timestamp, price)`` tuples (timestamps are server-native
            epoch seconds as received on the wire).
        """
        target = asset or self.api.current_asset
        if not target:
            return []
        return self.api.get_stream_ticks(target, limit)

    # ==================================================================
    # Historical candles
    # ==================================================================

    def get_historical_candles(
        self,
        active: str,
        period: int,
        start_time: Optional[int] = None,
        offset: Optional[int] = None,
        count_request: int = 1,
    ) -> Optional[list]:
        """Fetch OHLC history via WebSocket ``loadHistoryPeriod`` (see ``GetCandles``).

        End time for each request is derived from server-native time
        (``TimeSynchronizer.get_server_native_time()``): when ``start_time`` is
        omitted, ``current_time`` is ``floor(native_time/period)*period`` for
        ``period >= 60``, else ``int(native_time)``. That value is sent as the
        ``time`` field; ``offset`` is sent as-is as ``offset`` (candle count /
        window size expected by the server). Each iteration waits up to 10 s for
        ``history_data``.

        For ``period >= 60``, candle dicts from the server are merged and
        deduplicated by ``time``. For ``period < 60``, the response ticks are
        aggregated with :meth:`_ticks_to_candles`.

        Finally :meth:`_reconcile_with_realtime` merges ``updateHistoryNewFast``
        and stream ticks: drops any forming candle, may replace recent
        incomplete server bars, and may append gap-filled closed bars.

        Args:
            active: Asset symbol (e.g. ``\"EURUSD_otc\"``).
            period: Bar size in seconds (e.g. ``300`` for 5 minutes).
            start_time: Optional Unix **server-native** end bound for the first
                request (same units as ``get_server_native_time()``). If
                omitted, computed from synced time as above.
            offset: Value passed to ``loadHistoryPeriod`` as ``offset`` (see
                ``ws/channels/candles.py``). Use a positive candle count (this
                repo often uses ``45000`` or ``9000``). If ``None``, the wire
                message contains ``null``.
            count_request: Number of paginated requests; after each response,
                ``current_time`` is set to ``min(candle[\"time\"]) - period`` for
                the next round.

        Returns:
            Sorted list of candle dicts (keys include ``time``, ``open``,
            ``high``, ``low``, ``close``, ``volume`` when present), or ``None``
            if time not synced, not connected, timeout, or empty result.
        """
        all_data = []

        try:
            if not self.api.time_sync.is_synced():
                logger.error("Server time not synced yet")
                return None

            if start_time is None:
                native_time = self.api.time_sync.get_server_native_time()
                current_time = int((native_time // period) * period) if period >= 60 else int(native_time)
            else:
                current_time = int(start_time)

        except Exception as e:
            logger.error(f"Time init error: {e}")
            return None

        try:
            for i in range(count_request):
                if not self.check_connect():
                    logger.error("Not connected")
                    return None

                self.api.history_data = None
                self.api._history_data_event.clear()

                self.api.getcandles(active, period, offset, int(current_time))

                if self.api._history_data_event.wait(timeout=10):
                    if self.api.history_data and "data" in self.api.history_data:
                        data = self.api.history_data["data"]

                        if period < 60:
                            candles = self._ticks_to_candles(data, period)
                            if candles:
                                all_data.extend(candles)
                                if i < count_request - 1:
                                    current_time = min(c["time"] for c in candles) - period
                        else:
                            if data:
                                all_data.extend(data)
                                if i < count_request - 1:
                                    current_time = min(c["time"] for c in data) - period
                    else:
                        logger.warning(f"No data in response (request {i+1}/{count_request})")
                else:
                    logger.error("Timeout waiting for historical data")

            if not all_data:
                return None

            # Normalize and deduplicate
            for candle in all_data:
                if "timestamp" in candle and "time" not in candle:
                    candle["time"] = candle.pop("timestamp")
                if "volume" in candle and candle["volume"] is not None:
                    try:
                        candle["volume"] = int(float(candle["volume"]))
                    except (TypeError, ValueError):
                        pass

            all_data.sort(key=lambda x: x.get("time", 0))
            seen = set()
            unique = []
            for c in all_data:
                t = c.get("time")
                if t not in seen:
                    seen.add(t)
                    unique.append(c)

            unique = self._reconcile_with_realtime(unique, active, period)

            return unique

        except Exception as e:
            logger.error(f"get_historical_candles error: {e}", exc_info=True)
            return None

    def _ticks_to_candles(self, ticks: list, period: int) -> list:
        """Converts tick list to OHLC candles."""
        if not ticks:
            return []

        candles = []
        current = None

        for tick in sorted(ticks, key=lambda x: x.get("time", 0)):
            tick_time = tick.get("time", 0)
            candle_time = (tick_time // period) * period
            price = tick.get("price", tick.get("close", 0))

            if current is None or candle_time > current["time"]:
                if current is not None:
                    candles.append(current)

                vol = tick.get("volume")
                try:
                    vol = int(float(vol)) if vol is not None else 1
                except (TypeError, ValueError):
                    vol = 1

                current = {
                    "time": candle_time,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": vol,
                }
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                tv = tick.get("volume")
                try:
                    current["volume"] += int(float(tv)) if tv is not None else 1
                except (TypeError, ValueError):
                    current["volume"] += 1

        if current is not None:
            candles.append(current)

        return candles

    def _reconcile_with_realtime(self, candles: list, asset: str, period: int) -> list:
        """Reconciles server candles with real-time tick data.

        Three responsibilities:
        1. Strip the forming candle if the server included it.
        2. Detect and replace non-finalized server candles with tick-built
           versions.  A candle is considered non-finalized when it closed
           recently (< 2*period ago) OR its volume is abnormally low, AND
           tick data covers >70% of the candle period.
        3. Fill gaps between the last server candle and the currently forming
           candle using tick data.
        """
        if not candles:
            return candles

        native_now = self.api.time_sync.get_server_native_time()
        current_candle_start = int((native_now // period) * period)

        pre_len = len(candles)
        candles = [c for c in candles if c.get("time", 0) < current_candle_start]
        if len(candles) < pre_len:
            logger.debug(f"Stripped {pre_len - len(candles)} forming/future candle(s)")

        if not candles:
            return candles

        all_ticks: list = []

        fast_data = self.api._history_new_fast.get(asset)
        if fast_data and "history" in fast_data:
            for tick in fast_data["history"]:
                if isinstance(tick, (list, tuple)) and len(tick) >= 2:
                    all_ticks.append((float(tick[0]), float(tick[1])))

        stream_ticks = self.api.get_stream_ticks(asset)
        for ts, price in stream_ticks:
            all_ticks.append((float(ts), float(price)))

        if not all_ticks:
            return candles

        tick_candles: Dict[int, dict] = {}
        for ts, price in all_ticks:
            ct = int((ts // period) * period)
            if ct >= current_candle_start:
                continue

            if ct not in tick_candles:
                tick_candles[ct] = {
                    "time": ct,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "_tick_count": 1,
                    "_first_ts": ts,
                    "_last_ts": ts,
                }
            else:
                c = tick_candles[ct]
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["_tick_count"] += 1
                c["_last_ts"] = ts

        if not tick_candles:
            return candles

        recent_vols = [c.get("volume", 0) for c in candles[-20:] if c.get("volume", 0) > 0]
        vol_median = sorted(recent_vols)[len(recent_vols) // 2] if recent_vols else 0

        replaced = 0
        for i, srv in enumerate(candles):
            ct = srv.get("time")
            if ct not in tick_candles:
                continue

            tc = tick_candles[ct]
            coverage = (tc["_last_ts"] - tc["_first_ts"]) / period if period else 0
            if coverage < 0.7:
                continue

            srv_vol = srv.get("volume", 0) or 0
            candle_close_time = ct + period
            age = native_now - candle_close_time

            is_non_finalized = (
                age < 2 * period
                or (vol_median > 0 and srv_vol < vol_median * 0.6)
            )

            if is_non_finalized:
                candles[i] = {
                    "time": ct,
                    "open": srv.get("open", tc["open"]),
                    "high": max(srv.get("high", tc["high"]), tc["high"]),
                    "low": min(srv.get("low", tc["low"]), tc["low"]),
                    "close": tc["close"],
                    "volume": tc["_tick_count"],
                }
                replaced += 1

        if replaced:
            logger.info(f"Reconciled {replaced} non-finalized candle(s) with tick data")

        existing_times = {c.get("time") for c in candles}
        latest_time = max(existing_times)

        gap_filled = []
        for ct, tc in sorted(tick_candles.items()):
            if ct in existing_times or ct <= latest_time or ct >= current_candle_start:
                continue
            gap_filled.append({
                "time": ct,
                "open": tc["open"],
                "high": tc["high"],
                "low": tc["low"],
                "close": tc["close"],
                "volume": tc["_tick_count"],
            })

        if gap_filled:
            logger.info(f"Gap filled: +{len(gap_filled)} candle(s) from realtime ticks "
                        f"(after {latest_time}, before forming {current_candle_start})")
            candles.extend(gap_filled)
            candles.sort(key=lambda x: x.get("time", 0))
        else:
            native_gap = current_candle_start - latest_time
            if native_gap > period:
                logger.debug(f"No gap-fill ticks available (gap={native_gap // period} candles, "
                             f"fast_ticks={len(all_ticks)})")

        return candles

    # ==================================================================
    # Data processing helpers (for external use)
    # ==================================================================

    def process_candles_data(self, data: Union[dict, list], period: int) -> Optional[pd.DataFrame]:
        """Build a :class:`~pandas.DataFrame` from raw candle list or ``{\"data\": [...]}``.

        Sorts by ``time``, deduplicates on ``time``, converts ``time`` to int,
        then :meth:`_add_local_datetime` adds a string ``time`` column (local
        wall time) and keeps numeric Unix ``timestamp`` (server-native seconds
        with timezone correction applied per ``TimeSynchronizer._stream_tz_offset``).

        Args:
            data: List of candle dicts or a dict with key ``\"data\"``.
            period: Bar length in seconds; reserved for callers (not used for
                resampling in this method).

        Returns:
            DataFrame with columns ``time``, ``timestamp``, OHLC, optional
            ``volume``, or ``None`` on error / empty input.
        """
        try:
            if isinstance(data, list):
                data = {"data": data}

            ohlc_data = data.get("data", data)
            if not ohlc_data:
                return None

            df = pd.DataFrame(ohlc_data)
            if df.empty:
                return None

            required = ["time", "open", "high", "low", "close"]
            missing = [c for c in required if c not in df.columns]

            if missing:
                if "price" in df.columns:
                    df = df.rename(columns={"price": "close"})
                    df["open"] = df["close"]
                    df["high"] = df["close"]
                    df["low"] = df["close"]
                else:
                    logger.error(f"Missing columns: {missing}")
                    return None

            df["time"] = df["time"].astype(float).astype(int)
            df.sort_values("time", inplace=True)
            df.drop_duplicates(subset="time", keep="first", inplace=True)
            df.reset_index(drop=True, inplace=True)

            df = self._add_local_datetime(df)
            return df

        except Exception as e:
            logger.error(f"process_candles_data error: {e}")
            return None

    def _add_local_datetime(self, df: pd.DataFrame) -> pd.DataFrame:
        """Adds local datetime column from unix timestamps.

        Server timestamps carry a timezone offset (typically UTC+2).  We subtract
        that offset so the UTC interpretation is correct before converting to local.
        """
        try:
            df["timestamp"] = df["time"]

            tz_offset = self.api.time_sync._stream_tz_offset
            utc_times = df["time"] - tz_offset
            df["datetime"] = pd.to_datetime(utc_times, unit="s", utc=True)
            df["datetime"] = df["datetime"].dt.tz_convert(str(LOCAL_TIMEZONE))
            df["time"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
            df.drop("datetime", axis=1, inplace=True)

            base_cols = ["time", "timestamp", "open", "high", "low", "close"]
            extra_cols = ["volume"] if "volume" in df.columns else []
            cols = [c for c in base_cols if c in df.columns] + extra_cols
            return df[cols]
        except Exception as e:
            logger.error(f"Timezone conversion error: {e}")
            return df

    @staticmethod
    def process_candle(candle_data, period):
        """Sort and forward-fill candle rows; check spacing equals ``period``.

        Args:
            candle_data: Iterable of rows with a ``time`` column.
            period: Expected delta between consecutive ``time`` values (seconds).

        Returns:
            ``(df, is_continuous)`` where ``is_continuous`` is whether all
            ``diff(time)`` after the first row equal ``period``.
        """
        df = pd.DataFrame(candle_data)
        df.sort_values(by="time", ascending=True, inplace=True)
        df.drop_duplicates(subset="time", keep="first", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.ffill(inplace=True)
        if "symbol_id" in df.columns:
            df.drop(columns="symbol_id", inplace=True)
        diffs = df["time"].diff()
        is_continuous = (diffs[1:] == period).all()
        return df, is_continuous

    # ==================================================================
    # Trading
    # ==================================================================

    def buy(self, amount: float, active: str, direction: str, expiration: int):
        """Send ``openOrder`` via ``Buyv3`` (WebSocket ``sendMessage``).

        Builds ``requestId`` as ``direction + str(time.time())``, clears
        ``_order_open_event``, and waits up to 3 seconds for ``successopenOrder``
        with matching ``requestId`` in ``global_value.order_data``.

        Args:
            amount: Order size (passed as ``amount`` in the order body).
            active: Asset symbol.
            direction: Sent as ``action`` (e.g. ``\"call\"`` / ``\"put\"`` as in examples).
            expiration: Option duration in seconds, sent as ``time`` in ``Buyv3``.

        Returns:
            ``(order_dict, True)`` when the response dict matches ``requestId``,
            else ``({}, False)`` (timeout, mismatch, or exception).
        """
        try:
            request_id = direction + str(time.time())
            self.api._order_open_event.clear()
            global_value.order_data = None

            self.api.buyv3(amount, active, direction, expiration, request_id)

            if self.api._order_open_event.wait(timeout=3):
                if isinstance(global_value.order_data, dict) and global_value.order_data.get("requestId") == request_id:
                    return global_value.order_data, True
                else:
                    return {}, False

            logger.error("Order timeout")
            return {}, False

        except Exception as e:
            logger.error(f"Buy error: {e}", exc_info=True)
            return {}, False

    def check_win(self, id_number: str, callback: Optional[Callable] = None) -> None:
        """Queue an order id for the background worker to resolve from ``_order_results``.

        The worker calls :meth:`_check_win_internal` (polls ``api._order_results``
        up to ~120 s). If still ``pending`` before 120 s elapsed, the same
        order is re-queued. When a result with ``deals`` exists and status is
        not ``pending``, ``callback`` is invoked with ``result[\"deals\"][0]``.

        Args:
            id_number: Deal/order id string used as key in ``_order_results``
                (populated by ``successcloseOrder``).
            callback: Optional callable taking one deal dict.
        """
        self._check_win_queue.put({
            "id": id_number,
            "time": time.time(),
            "callback": callback,
        })

    def get_order_result(self, id_number: str) -> Optional[dict]:
        """Return ``api._order_results[id_number]`` if it exists and contains a non-empty ``deals`` list."""
        result = self.api._order_results.get(id_number)
        if result and "deals" in result and result["deals"]:
            return result
        return None

    def _check_win_worker(self):
        while True:
            try:
                order = self._check_win_queue.get()
                if order is None:
                    break

                order_id = order["id"]
                start = order["time"]
                callback = order.get("callback")

                result, status = self._check_win_internal(order_id)

                if status == "pending" and (time.time() - start) < 120:
                    self._check_win_queue.put(order)
                    time.sleep(0.1)
                    continue

                if callback and status != "pending" and result and "deals" in result:
                    try:
                        callback(result["deals"][0])
                    except Exception as e:
                        logger.error(f"check_win callback error for {order_id}: {e}")

            except Exception as e:
                logger.error(f"check_win worker error: {e}")
            finally:
                self._check_win_queue.task_done()

    def _check_win_internal(self, id_number: str, timeout: float = 120.0) -> Tuple[Optional[dict], str]:
        start = time.time()
        while time.time() - start < timeout:
            try:
                order_data = self.api._order_results.get(id_number)
                if order_data and "deals" in order_data:
                    return order_data, "closed"
            except Exception:
                pass
            time.sleep(0.1)

        return None, "timeout"

    def __del__(self):
        if hasattr(self, "_check_win_queue"):
            self._check_win_queue.put(None)
            if hasattr(self, "_check_win_thread"):
                self._check_win_thread.join(timeout=1)
