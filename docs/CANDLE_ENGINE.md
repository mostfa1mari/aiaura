# Candle Engine (Phase 5)

Builds OHLC candles from the canonical tick stream. Code:
`services/market_data/candles.py`; tests: `tests/test_candles.py`.

## Timeframes

`TIMEFRAMES = (1, 3, 5, 10, 15, 30, 60, 180, 300, 900)` seconds — i.e.
1s, 3s, 5s, 10s, 15s, 30s, 1m, 3m, 5m, 15m.

Several of these are **not** offered by Pocket Option's history API (notably
3s; see docs/POCKET_OPTION_API_AUDIT.md §6). They are constructed from ticks
here. Nothing is fabricated: a period with no ticks yields no candle.

## Bucketing and OHLC

- Bucket start: `floor(source_timestamp / tf) * tf` (UTC epoch seconds).
- Ticks are ordered by `source_timestamp`; within a bucket:
  open = first, close = last, high = max, low = min.
- `tick_count` = number of valid ticks in the bucket.
- Invalid prices (non-finite or `<= 0`) are skipped (they are still recorded
  and flagged by the quality layer on the raw data).
- `volume` is left `None` — Pocket Option volume is unreliable
  (docs/DATA_SCHEMA.md); `tick_count` is the activity measure.

## Completeness (no look-ahead)

- With `now` (current server time): a candle is complete iff
  `bucket_start + tf <= now`. The bucket containing `now` is **forming**.
- Without `now`: all buckets except the most recent are complete; the last is
  treated as forming (we cannot prove it closed without a clock).
- Forming candles carry `complete=False` and are excluded by default
  (`include_forming=True` to get them). **Forming candles must never be fed to
  training, backtests, or label generation** — that would leak the future.

## Gaps are real

Empty buckets produce no candle — the engine never forward-fills. Consumers
(feature engine, backtester) must treat a missing bucket as missing, and can
cross-reference the quality layer / connection events to see whether a gap was
an explained disconnect or genuine low activity.

## Batch vs incremental

| API | Use |
|---|---|
| `build_candles(ticks, tf, now=, include_forming=)` | Batch over a stored/collected tick list. |
| `CandleBuilder(asset, tf, on_candle=)` | Live single-timeframe; emits a completed candle the instant a later-bucket tick arrives; `forming()` exposes the in-progress candle. `flush(now)` promotes the forming candle to completed once its window has elapsed — call it periodically from the live loop so the final candle of a quiet period is not withheld. Ticks that arrive reordered *within* a bucket still yield correct open/close (keyed by timestamp, not arrival order). Stale ticks for an already-closed bucket are dropped and counted (`dropped_out_of_order`); the raw layer still keeps them. |
| `MultiTimeframeCandleBuilder(asset, timeframes, on_candle=)` | Fan the stream into one `CandleBuilder` per timeframe; usable directly as a provider tick listener; `flush(now)` flushes all timeframes. |

Batch and incremental produce the same completed candles for the same ticks
(intra-bucket order-independence + `flush(now)` give parity; verified in
tests). The live loop should call `flush(server_now)` about once per second so
elapsed candles are emitted even when the feed is momentarily quiet.
