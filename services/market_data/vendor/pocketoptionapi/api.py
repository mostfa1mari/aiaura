"""Module for Pocket Option API."""
import asyncio
import time
import json
import logging
import threading
import requests
import ssl
from collections import deque
from typing import List, Optional, Dict, Any

from pocketoptionapi.ws.client import WebsocketClient
from pocketoptionapi.ws.channels.get_balances import Get_Balances
from pocketoptionapi.ws.channels.ssid import Ssid
from pocketoptionapi.ws.channels.candles import GetCandles
from pocketoptionapi.ws.channels.buyv3 import Buyv3
from pocketoptionapi.ws.objects.time_sync import TimeSynchronizer
from pocketoptionapi.ws.objects.candles import Candles
from pocketoptionapi.ws.channels.change_symbol import ChangeSymbol
import pocketoptionapi.global_value as global_value

logger = logging.getLogger(__name__)

STREAM_BUFFER_SIZE = 500


class PocketOptionAPI(object):
    """Core API class: WebSocket transport, state storage, simple tick buffers."""

    time_sync = TimeSynchronizer()
    candles = Candles()

    # Order tracking
    _order_results: Dict[str, dict] = {}

    def __init__(self, proxies=None):
        self.websocket_client = None
        self.websocket_thread = None
        self.session = requests.Session()
        self.session.verify = False
        self.session.trust_env = False
        self.proxies = proxies
        self.buy_successful = None

        self.websocket_client = WebsocketClient(self)

        # Threading events for request-response patterns
        self._history_data_event = threading.Event()
        self._order_open_event = threading.Event()

        # Data set by WebSocket handlers in client.py
        self.history_data = None

        # Asset catalog from updateAssets
        self._assets_catalog: Dict[str, dict] = {}

        # Deals / orders
        self._opened_deals: list = []
        self._closed_deals: list = []
        self._pending_deals: list = []

        # Sentiment from chafor
        self._sentiment: Dict[str, float] = {}

        # Charts settings from updateCharts
        self._charts_data = None

        # Stream tick buffers: {asset: deque of (timestamp, price)}
        self._stream_buffers: Dict[str, deque] = {}

        # updateHistoryNewFast storage: {asset: dict}
        self._history_new_fast: Dict[str, dict] = {}

        # Current subscribed asset
        self.current_asset: Optional[str] = None
        self.current_period: Optional[int] = None

    @property
    def websocket(self):
        return self.websocket_client

    def send_websocket_request(self, name, msg, request_id="", no_force_send=True):
        data = f'42{json.dumps(msg)}'

        while (global_value.ssl_Mutual_exclusion or global_value.ssl_Mutual_exclusion_write) and no_force_send:
            pass
        global_value.ssl_Mutual_exclusion_write = True

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.websocket.send_message(data))

        logger.debug(f"send_websocket_request: name={name}, msg={str(msg)[:200]}")
        global_value.ssl_Mutual_exclusion_write = False

    def send_subfor(self, asset: str):
        """Sends subfor subscription from a sync context (threaded)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.websocket.send_subfor(asset))

    def start_websocket(self):
        global_value.websocket_is_connected = False
        global_value.check_websocket_if_error = False
        global_value.websocket_error_reason = None

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.websocket.connect())
        loop.run_forever()

        while True:
            try:
                if global_value.check_websocket_if_error:
                    return False, global_value.websocket_error_reason
                if global_value.websocket_is_connected is False:
                    return False, "Websocket connection closed."
                elif global_value.websocket_is_connected is True:
                    return True, None
            except:
                pass

    def connect(self):
        global_value.ssl_Mutual_exclusion = False
        global_value.ssl_Mutual_exclusion_write = False

        check_websocket, websocket_reason = self.start_websocket()
        if not check_websocket:
            return check_websocket, websocket_reason

        self.time_sync.server_timestamps = None
        while True:
            try:
                if self.time_sync.server_timestamps is not None:
                    break
            except:
                pass
        return True, None

    async def close(self, error=None):
        await self.websocket.on_close(error)

    def websocket_alive(self):
        return self.websocket_thread.is_alive()

    # ------------------------------------------------------------------
    # Channel properties
    # ------------------------------------------------------------------

    @property
    def get_balances(self):
        return Get_Balances(self)

    @property
    def buyv3(self):
        return Buyv3(self)

    @property
    def getcandles(self):
        return GetCandles(self)

    @property
    def change_symbol(self):
        return ChangeSymbol(self)

    # ------------------------------------------------------------------
    # Time sync helpers
    # ------------------------------------------------------------------

    @property
    def synced_datetime(self):
        try:
            if self.time_sync is not None:
                return self.time_sync.get_synced_datetime()
        except Exception as e:
            logger.error(f"synced_datetime error: {e}")
        return None

    @property
    def synced_timestamp(self):
        return self.time_sync.get_synced_time()

    # ------------------------------------------------------------------
    # Stream tick buffer (called from WebsocketClient)
    # ------------------------------------------------------------------

    def _on_stream_tick(self, asset: str, timestamp: float, price: float) -> None:
        """Stores a real-time tick in the per-asset ring buffer."""
        if asset not in self._stream_buffers:
            self._stream_buffers[asset] = deque(maxlen=STREAM_BUFFER_SIZE)
        self._stream_buffers[asset].append((timestamp, price))

    def _on_history_new_fast(self, data: dict) -> None:
        """Stores updateHistoryNewFast data for the asset."""
        asset = data.get("asset")
        if asset:
            self._history_new_fast[asset] = data
            logger.debug(f"updateHistoryNewFast stored for {asset}: "
                         f"{len(data.get('history', []))} ticks, period={data.get('period')}")

    def get_stream_ticks(self, asset: str, limit: Optional[int] = None) -> List[tuple]:
        """Returns recent (timestamp, price) ticks for an asset."""
        buf = self._stream_buffers.get(asset, deque())
        ticks = list(buf)
        if limit and limit > 0:
            ticks = ticks[-limit:]
        return ticks
