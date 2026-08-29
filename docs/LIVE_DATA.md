# Live Data

## Pipeline (current, Phase 1–3 slice)

```
Pocket Option OTC  (wss://*.po.market, Socket.IO)
        ↓
vendored pocketoptionapi  (auth, reconnect, updateStream)
        ↓  instance hooks: thread-safe sends + tick tee
PocketOptionMarketDataProvider  (canonical UTC ticks, supervision)
        ↓ listeners
   ├─ TickStore  →  data/raw/ticks/... (Parquet)
   └─ live consumers (monitor today; candle/feature engines next)
```

## Running

Prereq: `.env` with `PO_SSID` (see `.env.example` for capture instructions).

Continuous collector (headless; accumulates history for research/ML — the
prerequisite for validating any predictive edge). Leave it running:

```
.venv/Scripts/python scripts/collect.py --assets EURUSD_otc,GBPUSD_otc
```

`--all-otc` collects every available OTC pair (capped by `--max-assets`);
`--duration N` stops after N seconds (for cron-style bounded runs); status
lines show per-asset tick counts, quality grade, and connection health.

Live monitor (single asset, full-screen dashboard; persists ticks by default):

```
.venv/Scripts/python scripts/live_monitor.py --asset EURUSD_otc
```

Phase-30 milestone validation (10-minute soak incl. forced-reconnect test,
writes docs/LIVE_OTC_VALIDATION.md):

```
.venv/Scripts/python scripts/validate_live_otc.py --minutes 10
```

## Behavior notes

- One connected provider per process (vendored global state).
- Subscribe default period is 1 s (ticks stream regardless of period).
- Supervisor: re-subscribes after in-thread reconnects; rebuilds the client
  (with backoff) if the WS thread dies; stops retrying on auth failure and
  reports it via `health_check().detail`.
- `health_check().status`: GOOD / DEGRADED (no ticks >10 s or unsynced clock)
  / DOWN.
- Logs go to `logs/` with mandatory credential redaction; never disable
  `setup_logging`'s redaction when adding new entry points.
