# Data Schema

## Canonical tick (schema_version 1.0.0)

Defined in `services/market_data/provider.py::CanonicalTick`; persisted
columns in `services/market_data/storage.py::TICK_COLUMNS`.

| Column | Type | Meaning |
|---|---|---|
| `tick_id` | str | Unique per tick: `{asset}:{receive_ns}:{seq}` |
| `asset` | str | Provider symbol, e.g. `EURUSD_otc` |
| `price` | float | Tick price as received |
| `source_timestamp` | float | UTC epoch seconds, normalized (`wire − tz_offset`) |
| `received_timestamp` | float | UTC epoch seconds, local wall clock at ingestion |
| `latency_ms` | float | `(synced_server_now − source_timestamp) × 1000` — relative estimate against the synced-clock model, may be slightly negative |
| `provider` | str | `pocket_option` |
| `schema_version` | str | `1.0.0` |
| `raw_source_timestamp` | float | Wire timestamp exactly as received (server-native, typically UTC+2) — auditability / reprocessing |

Raw ticks are stored exactly as received: no deduplication, no gap filling,
no reordering. Quality flags are a derived layer (Phase 4), never mutations
of raw data.

## Storage layout

```
data/raw/ticks/{asset}/{YYYY-MM-DD}/part-{HHMMSS}-{rand}.parquet
```

- Partition key: UTC date of `source_timestamp`.
- Parquet files are append-only parts (Parquet cannot append in place); a
  finished day can later be compacted to a single file without changing the
  logical content. `TickStore.read_day()` reads either shape.
- Derived data (candles, features) will live under `data/candles/`,
  `data/features/` with their own schema docs when those phases land.

## Canonical candle (schema_version 1.0.0)

`CanonicalCandle`: `asset, timeframe_s, timestamp (UTC open time), open,
high, low, close, tick_count?, volume?, complete, provider, schema_version`.

- `complete=False` marks a forming candle; forming candles are never fed to
  training or backtests.
- Pocket Option "volume" is unreliable (mixed server volume / tick counts —
  see audit §4); treat `tick_count` as the meaningful activity measure.
