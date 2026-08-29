import asyncio
from datetime import datetime, timedelta, timezone
from collections import deque
from email.utils import parsedate_to_datetime
import websockets
from websockets.exceptions import WebSocketException
import json
import logging
import os
import ssl
import time
import re
import pocketoptionapi.global_value as global_value
from pocketoptionapi.constants import REGION
from pocketoptionapi.ws.objects.time_sync import TimeSynchronizer
from typing import Optional, Dict, Any, List
import backoff

logger = logging.getLogger(__name__)

_WS_VERSION = tuple(int(x) for x in websockets.__version__.split(".")[:2])
_HEADERS_KWARG = "additional_headers" if _WS_VERSION >= (13,) else "extra_headers"


class WebSocketError(Exception):
    pass

class AuthenticationError(WebSocketError):
    pass

class ConnectionError(WebSocketError):
    pass

class MessageError(WebSocketError):
    pass


class WebsocketClient:
    def __init__(self, api) -> None:
        self.api = api
        self._initialize_state()
        self._parse_ssid()

        self.region = REGION()
        self.url: Optional[str] = None
        self.connected = global_value.websocket_is_connected
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.base_delay = 1

        self._order_event = asyncio.Event()
        self.orders_data = {}

    def _initialize_state(self) -> None:
        self.wait_second_message = False
        self.successCloseOrder = False
        self.history_data_ready = False
        self.updateStream = False
        self.updateHistoryNew = False
        self.history_data = None
        self.message = None
        self.ssid = global_value.SSID
        self.websocket = None
        self._post_auth_done = False

    def _parse_ssid(self) -> None:
        """Parses uid, isDemo, and the full session from the raw SSID string.

        The session value often contains internal double-quotes (PHP serialized
        format like ``a:4:{s:10:"session_id";...}hash``).  A simple
        ``[^"]+`` regex truncates it at the first internal quote.  Instead we
        use a greedy pattern anchored to ``"isDemo"`` which only appears once.

        The session value captured by the regex still carries JSON escape
        sequences (``\\"`` for an embedded double-quote) because it was
        extracted from inside a JSON string literal.  We JSON-decode it so
        that ``_build_auth_message`` → ``json.dumps`` only applies one layer
        of escaping — matching what the browser originally sent.
        """
        try:
            ssid_str = self.ssid or ""

            is_demo_match = re.search(r'"isDemo"\s*:\s*(\d+)', ssid_str)
            uid_match = re.search(r'"uid"\s*:\s*(\d+)', ssid_str)

            self.is_demo = bool(int(is_demo_match.group(1))) if is_demo_match else True
            self.uid = int(uid_match.group(1)) if uid_match else None

            session_match = re.search(r'"session"\s*:\s*"(.*)",\s*"isDemo"', ssid_str)
            session_raw = session_match.group(1) if session_match else ""

            # JSON-decode the captured session to strip one layer of escaping.
            # e.g. captured \" (backslash+quote) → actual " character.
            # json.dumps in _build_auth_message will then re-encode it correctly.
            try:
                self.session = json.loads('"' + session_raw + '"')
            except (json.JSONDecodeError, ValueError):
                # Fallback for sessions without JSON escapes (simple demo tokens).
                self.session = session_raw

            logger.info(
                "SSID parsed: %s mode, uid=%s, session_len=%d",
                "DEMO" if self.is_demo else "REAL",
                self.uid,
                len(self.session),
            )

        except Exception as e:
            logger.warning(f"Error parsing SSID, defaulting to demo: {e}")
            self.is_demo = True
            self.uid = None
            self.session = ""

    def _build_auth_message(self) -> str:
        """Builds the auth message with properly JSON-escaped session value.

        ``json.dumps`` handles escaping internal double-quotes in the PHP
        session string so the server receives valid JSON.
        """
        auth_payload = {
            "session": self.session,
            "isDemo": 1 if self.is_demo else 0,
            "uid": self.uid,
            "platform": 2,
            "isFastHistory": True,
            "isOptimized": True,
        }
        return f'42{json.dumps(["auth", auth_payload])}'

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @backoff.on_exception(
        backoff.expo,
        (WebSocketException, ConnectionError),
        max_tries=5,
        max_time=30,
    )
    async def websocket_listener(self, ws):
        try:
            async for message in ws:
                await self.on_message(message)
        except Exception as e:
            logger.error(f"websocket_listener error: {e}")
            global_value.websocket_is_connected = False
            raise

    async def send_ping(self, ws):
        ping_interval = 20
        last_ping_time = time.time()

        while True:
            try:
                current_time = time.time()
                if current_time - last_ping_time >= ping_interval:
                    if not global_value.websocket_is_connected:
                        break
                    try:
                        await ws.send('42["ps"]')
                        last_ping_time = current_time
                    except Exception as e:
                        logger.error(f"Ping error: {e}")
                        global_value.websocket_is_connected = False
                        break
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ping loop error: {e}")
                global_value.websocket_is_connected = False
                break

    async def connect(self):
        # AIAURA-PATCH(tls): upstream disables certificate verification, which
        # exposes the session credential to man-in-the-middle interception.
        # Verify TLS by default; set PO_TLS_INSECURE=1 only if the endpoint's
        # certificate cannot be validated in this environment.
        ssl_context = ssl.create_default_context()
        if os.environ.get("PO_TLS_INSECURE", "0") == "1":
            logger.warning("PO_TLS_INSECURE=1: TLS certificate verification is DISABLED")
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            await self.api.close("TRY CLOSE CONNECTION")
        except Exception:
            pass

        available_servers = self._filter_servers()
        if not available_servers:
            raise ConnectionError("No servers available for this account type")

        # For real accounts: probe latencies in parallel and sort fastest-first
        # (this is the automatic zone/region detection — no need to hard-code regions).
        # For demo: order is already deterministic (primary EU first), no probe needed.
        if not self.is_demo and len(available_servers) > 1:
            available_servers = await self._sort_servers_by_latency(available_servers, ssl_context)

        while (
            not global_value.websocket_is_connected
            and not global_value.check_websocket_if_error   # stop immediately on auth failure
            and self.reconnect_attempts < self.max_reconnect_attempts
        ):
            for url in available_servers:
                if global_value.websocket_is_connected:
                    break
                if global_value.check_websocket_if_error:   # auth rejected — no point trying more
                    break
                try:
                    self.delay = min(self.base_delay * (2 ** self.reconnect_attempts), 10)
                    logger.info(f"Connection attempt {self.reconnect_attempts + 1} to: {url} ({'REAL' if not self.is_demo else 'DEMO'})")

                    ws_kwargs = {
                        _HEADERS_KWARG: self._get_headers(),
                        "open_timeout": 8,   # reduced from 30s — fast fail on unreachable servers
                        "ping_interval": None,
                        "ping_timeout": None,
                        "close_timeout": 5,
                        "max_size": 1_000_000,
                        "compression": None,
                    }
                    async with websockets.connect(
                        url, ssl=ssl_context, **ws_kwargs,
                    ) as ws:
                        self.websocket = ws
                        self.url = url
                        self._post_auth_done = False

                        self._sync_time_from_http_date(ws)

                        try:
                            # Python 3.11+: asyncio.wait() requires Tasks, not bare coroutines.
                            tasks = [
                                asyncio.create_task(self.websocket_listener(ws)),
                                asyncio.create_task(self.send_ping(ws)),
                            ]
                            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in pending:
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                            for task in done:
                                exc = task.exception()
                                if exc:
                                    raise exc
                        except Exception as e:
                            logger.error(f"WebSocket tasks error: {e}")
                            continue

                except Exception as e:
                    logger.warning(f"Connection error: {e}")
                    self.reconnect_attempts += 1
                    await asyncio.sleep(self.delay)
                    continue

            if (
                not global_value.websocket_is_connected
                and not global_value.check_websocket_if_error
            ):
                logger.warning(f"Could not connect to any server, retrying in {self.delay}s...")
                await asyncio.sleep(self.delay)

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            raise ConnectionError("Max reconnection attempts reached")

    def _filter_servers(self) -> List[str]:
        """Return the ordered candidate server list for the current SSID type.

        Demo  → EU servers only, deterministic order (primary first).
        Real  → all real servers in base order; connect() will sort by latency.
        """
        if self.is_demo:
            return self.region.get_demo_servers(randomize=False)
        return self.region.get_real_servers(randomize=False)

    async def _sort_servers_by_latency(
        self,
        servers: List[str],
        ssl_context: ssl.SSLContext,
        probe_timeout: float = 1.5,
    ) -> List[str]:
        """Probe all servers in parallel via TCP and sort fastest-first.

        Unreachable servers are appended at the end of the list so they are
        still tried as a last resort rather than silently dropped.
        """
        from urllib.parse import urlparse

        async def _probe(url: str) -> tuple:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or 443
            t0 = time.monotonic()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_context),
                    timeout=probe_timeout,
                )
                latency = time.monotonic() - t0
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
                return latency, url
            except Exception:
                return float("inf"), url

        results = await asyncio.gather(*[_probe(u) for u in servers])
        results = sorted(results, key=lambda x: x[0])

        reachable = [(lat, url) for lat, url in results if lat != float("inf")]
        unreachable = [(lat, url) for lat, url in results if lat == float("inf")]

        if reachable:
            logger.info(
                "Server latency probe: %s",
                ", ".join(
                    f"{url.split('/')[2]} ({lat * 1000:.0f}ms)"
                    for lat, url in reachable
                ),
            )
        if unreachable:
            logger.debug(
                "Unreachable servers (will try last): %s",
                ", ".join(url.split("/")[2] for _, url in unreachable),
            )

        return [url for _, url in reachable] + [url for _, url in unreachable]

    def _sync_time_from_http_date(self, ws) -> None:
        """Extracts the Date header from the WS upgrade response for initial time sync."""
        try:
            headers = getattr(ws, 'response_headers', None)
            if headers is None:
                resp = getattr(ws, 'response', None)
                if resp is not None:
                    headers = getattr(resp, 'headers', None)
            if headers is None:
                logger.debug("No response headers available for initial time sync")
                return

            date_str = headers.get('Date')
            if not date_str:
                logger.debug("No Date header in WS upgrade response")
                return

            server_dt = parsedate_to_datetime(date_str)
            server_ts = server_dt.timestamp()
            self.api.time_sync.synchronize(server_ts)
            logger.info(f"Initial time sync from HTTP Date header: {date_str} -> {server_ts:.0f}")
        except Exception as e:
            logger.warning(f"Could not sync time from HTTP Date header: {e}")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Origin": "https://pocketoption.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "es-ES,es;q=0.9",
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
        }

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_message(self, message: str) -> None:
        if not global_value.websocket_is_connected or self.websocket is None:
            raise ConnectionError("No active WebSocket connection")
        if message is not None:
            self.message = message
            try:
                await self.websocket.send(message)
                logger.debug(f"Sent: {message[:200]}")
            except Exception as e:
                logger.error(f"Send error: {e}")
                raise ConnectionError(f"Send error: {e}")

    async def send_subfor(self, asset: str) -> None:
        """Sends the subfor subscription message for a given asset."""
        msg = f'42{json.dumps(["subfor", asset])}'
        await self.send_message(msg)
        logger.debug(f"subfor sent for {asset}")

    async def on_message(self, message) -> None:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
                json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.debug(f"Undecodable binary message ({len(message)} bytes)")
            return

        await self._handle_text_message(message)

    async def on_error(self, error):
        logger.error(error)
        global_value.websocket_error_reason = str(error)
        global_value.check_websocket_if_error = True

    async def on_close(self, error):
        logger.debug(f"WebSocket closed: {error}")
        global_value.websocket_is_connected = False

    # ------------------------------------------------------------------
    # Text message routing (Engine.IO / Socket.IO handshake + events)
    # ------------------------------------------------------------------

    async def _handle_text_message(self, message: str) -> None:
        ws = self.websocket
        if ws is None:
            return
        if message.startswith('0{"sid":'):
            logger.debug("Handshake: open packet, sending 40")
            await ws.send("40")
        elif message == "2":
            await ws.send("3")
        elif message.startswith('40{"sid":'):
            logger.debug("Handshake: namespace connected, sending auth")
            auth_msg = self._build_auth_message()
            # AIAURA-PATCH(log): upstream logged auth_msg[:120], which leaks
            # ~96 chars of the live session (incl. session_id) in cleartext.
            # A truncated credential defeats exact-match log redaction by
            # construction, so never emit the value — log only its length.
            logger.info("Sending auth message (%d bytes)", len(auth_msg))
            await ws.send(auth_msg)
        elif message == "41":
            logger.error("Handshake: namespace disconnect (41) — credentials rejected")
            global_value.websocket_is_connected = False
            global_value.check_websocket_if_error = True
            global_value.websocket_error_reason = "Unauthorized: credentials rejected by server (namespace disconnect 41)"
            await ws.close()
        elif message.startswith("451-["):
            await self._handle_451_message(message)
        elif message.startswith("42") and "NotAuthorized" in message:
            await self._handle_unauthorized()

    # ------------------------------------------------------------------
    # 451 binary-event handler
    # ------------------------------------------------------------------

    async def _handle_451_message(self, message: str) -> None:
        """Handles Socket.IO binary events: 451-["type", {_placeholder, num}]"""
        try:
            json_part = message.split("-", 1)[1]
            msg_data = json.loads(json_part)
            msg_type = msg_data[0]
            logger.debug(f"451 message type: {msg_type}")

            ws = self.websocket
            if ws is None:
                return
            if len(msg_data) > 1 and isinstance(msg_data[1], dict) and msg_data[1].get("_placeholder"):
                try:
                    binary_data = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(binary_data, bytes):
                        await self._decode_binary_message(binary_data, msg_type)
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout waiting for binary data: {msg_type}")
                except Exception as e:
                    logger.error(f"Error receiving binary data for {msg_type}: {e}")

        except Exception as e:
            logger.error(f"Error processing 451 message: {e}")

    async def _decode_binary_message(self, binary_data: bytes, msg_type: str) -> Any:
        """Decodes and dispatches binary message payloads by type."""
        try:
            decoded = binary_data.decode("utf-8")
            data = json.loads(decoded)
            logger.debug(f"Binary decoded ({msg_type}): {str(data)[:200]}")

            if msg_type == "successauth":
                logger.info("Authentication successful")
                if isinstance(data, dict):
                    global_value.account_id = data.get("id") or data.get("accountId")
                await self._mark_connected_and_post_auth()
                return data

            elif msg_type == "updateAssets":
                await self._handle_assets_update(data)
                return data

            elif msg_type == "successupdateBalance":
                if isinstance(data, dict):
                    await self._handle_balance_update(data)
                return data

            elif msg_type == "updateStream":
                # AIAURA-PATCH(stream): upstream only processed data[0]; a
                # batched message would silently drop every tick after the
                # first. Process all rows.
                if isinstance(data, list):
                    for stream_data in data:
                        if isinstance(stream_data, list) and len(stream_data) >= 3:
                            asset, timestamp, price = stream_data[0], stream_data[1], stream_data[2]
                            self.api.time_sync.synchronize(timestamp)
                            self.api._on_stream_tick(asset, timestamp, price)
                return data

            elif msg_type == "loadHistoryPeriodFast":
                if isinstance(data, dict):
                    self.api.history_data = data
                    self.api._history_data_event.set()
                    logger.debug(f"loadHistoryPeriodFast received: {len(data.get('data', []))} candles")
                return data

            elif msg_type == "updateHistoryNewFast":
                if isinstance(data, dict) and "asset" in data:
                    self.api._on_history_new_fast(data)
                return data

            elif msg_type == "successopenOrder":
                if isinstance(data, dict):
                    order_id = data.get("id")
                    if order_id:
                        self.orders_data[order_id] = data
                    global_value.order_data = data
                    global_value.result = True
                    self.api._order_open_event.set()
                    logger.debug(f"Order opened: {data.get('id')}")
                else:
                    global_value.result = False
                    self.api._order_open_event.set()
                return data

            elif msg_type == "successcloseOrder":
                if isinstance(data, dict) and "deals" in data and isinstance(data["deals"], list):
                    for deal in data["deals"]:
                        if "id" in deal:
                            self.api._order_results[deal["id"]] = {"deals": [deal]}
                return data

            elif msg_type == "updateOpenedDeals":
                if isinstance(data, list):
                    self.api._opened_deals = data
                    logger.debug(f"Open deals updated: {len(data)} deals")
                return data

            elif msg_type == "updateClosedDeals":
                if isinstance(data, list):
                    self.api._closed_deals = data
                    logger.debug(f"Closed deals updated: {len(data)} deals")
                return data

            elif msg_type == "successupdatePending":
                if isinstance(data, list):
                    self.api._pending_deals = data
                return data

            elif msg_type == "chafor":
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, list) and len(item) >= 2:
                            self.api._sentiment[item[0]] = item[1]
                return data

            elif msg_type == "updateCharts":
                self.api._charts_data = data
                return data

            else:
                logger.debug(f"Unhandled binary message type: {msg_type}")

            return data

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {msg_type}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing binary {msg_type}: {e}")
            return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_assets_update(self, data) -> None:
        """Parses the updateAssets payload into a structured catalog.

        NOTE: Does NOT mark the connection as authenticated — only
        ``successauth`` is authoritative for that. In real-mode servers the
        assets catalog can arrive *before* auth is confirmed.
        """
        logger.debug("Assets update received")
        if isinstance(data, list):
            catalog = {}
            for asset_row in data:
                if isinstance(asset_row, list) and len(asset_row) >= 6:
                    asset_id = asset_row[0]
                    symbol = asset_row[1]
                    name = asset_row[2] if len(asset_row) > 2 else symbol
                    category = asset_row[3] if len(asset_row) > 3 else "unknown"
                    payout = asset_row[5] if len(asset_row) > 5 else 0
                    is_available = asset_row[14] if len(asset_row) > 14 else False
                    timeframes = asset_row[15] if len(asset_row) > 15 else []

                    catalog[symbol] = {
                        "id": asset_id,
                        "name": name,
                        "category": category,
                        "payout": payout,
                        "is_available": is_available,
                        "timeframes": timeframes,
                        "raw": asset_row,
                    }
            self.api._assets_catalog = catalog
            logger.info(f"Assets catalog loaded: {len(catalog)} assets")

    async def _handle_balance_update(self, data: Dict[str, Any]) -> None:
        if "uid" in data:
            global_value.balance_id = data["uid"]
        if "balance" in data:
            global_value.balance = data["balance"]
        if "isDemo" in data:
            global_value.balance_type = data["isDemo"]
        global_value.balance_updated = True
        logger.debug(f"Balance updated: {data.get('balance')}")

    async def _handle_unauthorized(self) -> None:
        logger.error("Unauthorized: please use a valid SSID")
        global_value.ssl_Mutual_exclusion = False
        global_value.websocket_is_connected = False
        global_value.check_websocket_if_error = True
        global_value.websocket_error_reason = "Unauthorized: please use a valid SSID"
        if self.websocket is not None:
            await self.websocket.close()

    async def on_open(self) -> None:
        if global_value.websocket_is_connected:
            return
        logger.info(f"Authenticated & connected to: {self.url}")
        global_value.websocket_is_connected = True

    async def _mark_connected_and_post_auth(self) -> None:
        await self.on_open()
        if self._post_auth_done:
            return
        self._post_auth_done = True
        try:
            await self._send_post_auth_messages()
        except Exception as e:
            logger.error(f"Post-auth messages error: {e}")

    async def _send_post_auth_messages(self) -> None:
        ws = self.websocket
        if ws is None:
            return
        messages = [
            '42["indicator/load"]',
            '42["favorite/load"]',
            '42["price-alert/load"]',
        ]
        for msg in messages:
            await ws.send(msg)
