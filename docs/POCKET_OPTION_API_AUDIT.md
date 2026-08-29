# Pocket Option API Audit (Phase 0)

Audit of the unofficial client library used as AI AURA's OTC market-data
source. Every claim below was verified against the vendored source snapshot.

- **Upstream**: https://github.com/chema-creator/PocketOptionApi
- **Audited commit**: `5b7418a51f577732a4e7f7aeeecf969610bda3cb`
- **Library version**: `PocketOption.__version__ = "2.0.0"` (setup.py says 0.0.1 — inconsistent, cosmetic)
- **License**: MIT per README (repo ships no LICENSE file)
- **Vendored at**: `services/market_data/vendor/pocketoptionapi/` with two marked patches (see `VENDOR_NOTES.md`)
- **Audit date**: 2026-08-29

## 1. Architecture

Three layers, one WebSocket:

| Layer | File | Role |
|---|---|---|
| `PocketOption` | `stable_api.py` (805 LOC) | Public synchronous API. Spawns the WS thread, exposes connect/subscribe/history/ticks (+ order methods AI AURA never calls). |
| `PocketOptionAPI` | `api.py` (207 LOC) | State container: per-asset tick ring buffers (**maxlen 500**), asset catalog, history cache, threading events. |
| `WebsocketClient` | `ws/client.py` (~660 LOC) | Socket.IO/Engine.IO framing, SSID parse, server selection, auth handshake, reconnection, message dispatch. |

The WS runs on a dedicated thread with its own asyncio loop
(`PocketOption._run_websocket_thread`, loop stored in `_io_loop`).
Transport: `websockets` (v12/v13+ both handled). Frames are Socket.IO text
events plus `451-` binary events (JSON payload arrives as a follow-up binary
frame).

## 2. Authentication

- Credential: the full Socket.IO auth frame `42["auth",{"session":"...","isDemo":N,"uid":...}]`, captured from a logged-in browser (the "SSID").
- `_parse_ssid()` regex-extracts `session`, `isDemo`, `uid`; real-account sessions are PHP-serialized strings with embedded quotes; parse failure silently **defaults to demo**.
- `_build_auth_message()` re-encodes with `json.dumps` and adds `platform: 2, isFastHistory: true, isOptimized: true`.
- Handshake: server `0{"sid"...}` → client `40` → server `40{"sid"...}` → client sends auth → server `successauth` (authoritative; `updateAssets` may arrive before auth on real servers).
- Auth failure paths: Socket.IO `41` namespace disconnect, or `42...NotAuthorized` → sets `check_websocket_if_error` + reason "Unauthorized...", stops retrying.
- Demo vs real selects the server pool: demo → `demo-api-eu.po.market` / `try-demo-eu.po.market`; real → 4 endpoints (EU×2, US×2) with a parallel TCP latency probe, fastest first.
- Keepalive: `42["ps"]` every ~20 s; Engine.IO ping `2`→`3` handled.

## 3. Real-time data mechanism

- `subscribe(asset, period)` sends `changeSymbol {asset, period}` + `subfor asset`.
- Server then emits `updateStream` events: rows `[asset, timestamp, price]`.
  Timestamps are **server-native epoch seconds (observed UTC+2)**, may be fractional.
- Ticks land in `PocketOptionAPI._stream_buffers[asset]`, a `deque(maxlen=500)` — a poll-only ring buffer with **no callback hook**. AI AURA's provider tees `_on_stream_tick` (instance-level override) into its own canonical buffers, so no polling race and no 500-tick cap.
- `updateHistoryNewFast` supplies recent tick history per asset (used by the library for candle reconciliation/gap-fill).
- Sentiment (`chafor`), balance (`successupdateBalance`), deals lists are pushed automatically; read-only getters exist.

## 4. Historical data mechanism

- `get_historical_candles(active, period, start_time, offset, count_request)` sends `loadHistoryPeriod {asset, index, offset, period, time}`; response event `loadHistoryPeriodFast`, waited on a threading event (10 s timeout per page).
- `time` must be **server-native**; the library derives it from `TimeSynchronizer.get_server_native_time()`. History requires time sync (`is_time_synced()`), else returns `None`.
- `offset` is a candle-count/window field; the repo empirically uses large values (9000/45000) to avoid server timeouts. AI AURA uses 45000.
- For `period < 60` the server returns ticks; the library aggregates them into candles client-side (`_ticks_to_candles`). For `period >= 60` it returns candle dicts (`time, open, high, low, close, volume?`).
- `_reconcile_with_realtime` then: strips the forming candle, replaces recent non-finalized bars with tick-built OHLC when tick coverage > 70 %, and gap-fills from ticks. Volume on replaced bars becomes **tick count** — volume semantics are therefore mixed and unreliable; AI AURA treats it as tick-count-ish, not traded volume.
- Pagination steps `time` backwards from the oldest candle received.

## 5. Time synchronization

- Initial offset from the HTTP `Date` header of the WS upgrade; refined continuously from `updateStream` timestamps via a trimmed-mean over a 20-sample window.
- The stream's timezone offset (UTC+N, observed +2) is auto-detected on the first tick that deviates > 30 min from expectation, then stored in `_stream_tz_offset`.
- `get_synced_time()` = estimated server time in UTC; `get_server_native_time()` adds the stream offset back (for history requests).
- **Canonical rule for AI AURA**: `source_timestamp(UTC) = wire_timestamp − _stream_tz_offset`.
- Caveat: `TimeSynchronizer` is a **class attribute** of `PocketOptionAPI` — shared across instances (survives provider rebuilds; harmless, mildly useful).

## 6. Supported candle periods

`CANDLE_SIZES = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400, 604800, 2592000]`.
AI AURA horizons not in this list (3 s) must be built from ticks by our own candle engine (Phase 5) — never fabricated.

## 7. OTC symbol handling

- OTC symbols use the `_otc` suffix (e.g. `EURUSD_otc`). `constants.ACTIVES` has static name→id hints only.
- The live catalog comes from `updateAssets`: per symbol `id, name, category, payout, is_available, timeframes, raw`. Field positions are **index-based** (`row[5]`=payout, `row[14]`=is_available, `row[15]`=timeframes) — brittle if the platform reorders; validation checks plausibility at runtime.
- Availability must be read live (`is_available`), never hardcoded. AI AURA's provider raises `AssetUnavailableError` otherwise.

## 8. Reconnect behavior

| Mechanism | Behavior |
|---|---|
| Connect loop | Walks the server list; exponential backoff (1 s base, 10 s cap); **gives up after 5 failed attempts** and the WS thread dies. |
| In-connection drop | `async with` exits → next URL is tried on the same thread → re-auth happens automatically. `reconnect_attempts` is NOT incremented for this path. |
| Auth failure | Terminal: error flag set, no further retries. |
| Read loop | `@backoff` retries transient read errors (5 tries / 30 s). |

**Gaps AI AURA must (and does) cover in the provider layer**:
1. After an in-thread reconnect the server-side `subfor` subscription is gone — the library does **not** re-subscribe. Our supervisor re-sends subscriptions on every disconnect→connect transition.
2. After 5 hard failures the thread dies and the instance is unusable (`reconnect_attempts` is never reset) — our supervisor rebuilds a fresh `PocketOption` instance with backoff.
3. Auth-failure ("Unauthorized") is treated as terminal by the supervisor: no hammering; user must refresh the SSID.

## 9. Order-execution surface (identified; NEVER used)

| Symbol | Location | Wire message |
|---|---|---|
| `PocketOption.buy()` | stable_api.py | `openOrder` via Buyv3 |
| `PocketOption.check_win()` / `get_order_result()` | stable_api.py | reads `successcloseOrder` results |
| `Buyv3`, `Buyv3_by_raw_expired` | ws/channels/buyv3.py | `openOrder` / `binary-options.open-option` |
| `PocketOptionAPI.buyv3` property | api.py | — |

AI AURA never imports or calls any of these; `tests/test_no_order_execution.py`
AST-scans all AI AURA code (vendor excluded) for these identifiers on every
test run. Note: `PocketOption.__init__` always starts a `check_win` worker
thread; it idles on an empty queue and can never place an order by itself.
Passive events (`updateOpenedDeals` etc.) may still arrive from the server if
the user trades manually in their browser — they are stored, never acted on.

## 10. Known limitations

1. **Process-global state** (`global_value.py`): SSID, connection flags, balance — one connection per process. Provider enforces this.
2. **Unsafe cross-loop sends**: `send_websocket_request` / `send_subfor` create a fresh event loop in the calling thread and run `ws.send()` there, while the socket belongs to the WS thread's loop. Works by accident, can corrupt frames under concurrency. Provider overrides both with `asyncio.run_coroutine_threadsafe` onto the client's own loop.
3. **500-tick ring buffer** with no push hook (solved by the tee, above).
4. `updateStream` handler originally dropped all rows but the first in a batched message (patched: `AIAURA-PATCH(stream)`).
5. Busy-wait spin lock in `send_websocket_request` (bypassed by our override).
6. `start_websocket`/`connect` in `api.py` contain unreachable/broken code paths (dead `while True` after `run_forever`); the actually-used path is `PocketOption.connect()`.
7. `setup.py` pins `websocket-client==0.56` which is never imported (dead dependency); real deps are websockets/pandas/tzlocal/backoff/requests.
8. Volume semantics are inconsistent (server volume vs tick counts after reconciliation).
9. Asset-catalog parsing is positional (index-based) — fragile across platform changes.
10. No unsubscribe channel exists; AI AURA sends the platform's `unsubfor` frame directly (best effort).
11. Sub-second data: ticks are the finest granularity; there is no server-side sub-second candle feed.

## 11. Security concerns

1. **TLS verification disabled upstream** (`CERT_NONE` in `connect()`, `session.verify=False` in api.py) — exposes the live session credential to MITM. **Patched**: verification ON by default (`AIAURA-PATCH(tls)`), `PO_TLS_INSECURE=1` escape hatch. The `requests` session is never actually used for calls.
2. **Credential partially logged upstream**: `client.py` originally logged `Sending auth: {auth_msg[:120]}...` at INFO — that prefix leaks ~96 chars of the session (incl. `session_id`) in cleartext, and because it is *truncated* it defeats exact-value log redaction. **Patched** at the source (`AIAURA-PATCH(log)`: the value is no longer logged, only its byte length). Defense in depth: AI AURA's mandatory log-redaction filter (`services/market_data/security.py`) also *structurally* redacts any `"session"` field value — truncated or not — from every log handler, and the live-validation gate scans its own log with the same truncation-proof check (`scan_for_leaks`) so it cannot report a false "no credentials" PASS. All verified by tests (`tests/test_security.py`).
3. The SSID is a full session credential: whoever holds it can act as the account. Storage rules: `.env` only, gitignored; never in chat, code, docs, or logs.
4. Spoofed browser headers (fixed Chrome UA, `Origin: pocketoption.com`) — inherent to an unofficial client.
5. No telemetry/exfiltration found: the library talks **only** to `*.po.market` WebSocket endpoints; no other hosts, no exec/eval, no obfuscated code (full source reviewed).

## 12. Risk statement (owner-accepted 2026-08-29)

This is an **unofficial** client driving a session credential against Pocket
Option's private WebSocket API. Using it may violate Pocket Option's terms of
service and can lead to session invalidation or account suspension. The feed
is the broker's own OTC pricing — a closed, broker-controlled price source.
AI AURA uses it read-only, for research and manually-executed signals, with
no order execution, no profitability claims, and no martingale logic.
