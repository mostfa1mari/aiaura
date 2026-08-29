# Data Quality (Phase 4)

Quality is a **derived** view over canonical ticks. It never mutates raw
data — raw ticks are immutable history (docs/DATA_SCHEMA.md). Code:
`services/market_data/quality.py`; tests: `tests/test_quality.py`.

## Entry points

| API | Use |
|---|---|
| `analyze_ticks(ticks, ...)` | Batch `QualityReport` over a captured sequence (e.g. a stored day, a validation run). |
| `TickQualityMonitor` | Incremental; safe as a provider tick listener; keeps running counters + recent issues per asset for the live monitor / dashboard. |
| `summarize_connection_events(events)` | Interruption/reconnect/resubscribe/auth-failure counts from the provider's `connection_events`. |

Both entry points run the **same** per-tick algorithm (`_QualityAccumulator`),
so the batch report and the live monitor produce identical counters for the
same tick sequence. The only intended difference: the future-timestamp check
runs only in batch mode with an explicit `now` (a live tick's arrival instant
*is* "now", so the check is not meaningful there). `error_count`, `is_clean`,
and `quality_grade` are derived from the **exact integer counters**, never the
issues list — which is a bounded display buffer (first N in batch, most-recent
N in the monitor).

## Detected conditions

| Condition | Rule | Severity |
|---|---|---|
| invalid price | not finite, or `<= 0` | error |
| out-of-order | `source_timestamp` decreased vs previous | error |
| future timestamp | `source_timestamp > now + tolerance` (only when `now` is supplied) | error |
| gap | inter-tick interval `> gap_s` (default 5 s) | warning |
| abnormal gap | interval `> abnormal_gap_s` (default 30 s) | error |
| price spike | single-tick move `> spike_pct` (default 0.5 %) vs last valid price | warning |
| duplicate | identical `(source_timestamp, price)` as previous | info |

`max_gap_s` and `mean_interval_s` (mean over non-negative intervals only) are
also reported. Thresholds are constructor/keyword args — tune per asset and
timeframe; the defaults are conservative starting points, not claims about
what is "normal" for a given OTC symbol.

## Grade

`QualityReport.quality_grade`:
- **GOOD** — no errors and no abnormal gaps.
- **FAIR** — a few errors (`<= tick_count/100`).
- **POOR** — otherwise, or no ticks.

Coarse, for dashboards only. Downstream code should look at the specific
counters, not the grade.

## Connection interruptions / reconnections

The provider records lifecycle transitions in `connection_events`
(`disconnect_detected`, `reconnected_in_thread`, `rebuilt`, `resubscribed`,
`stale_resubscribe`, `auth_failed_terminal`, ...).
`summarize_connection_events` rolls these into interruption / reconnect /
resubscribe / auth-failure counts. A data gap in the tick stream that lines
up with a `disconnect_detected` → `reconnected_in_thread` pair is an
*explained* gap, not a data-integrity problem.

## Relationship to raw data

Quality flags are computed on read; they are never written back into the raw
Parquet. A future normalized layer (`data/normalized/`) may persist
quality-annotated data separately, but the raw tick files remain the source
of truth.
