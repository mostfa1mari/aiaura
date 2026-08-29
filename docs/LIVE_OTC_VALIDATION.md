# LIVE OTC VALIDATION — Phase 30 first milestone

- **Run at**: 2026-08-29 16:42:43 UTC
- **Requested asset**: EURUSD_otc
- **Requested duration**: 10.0 min
- **Provider**: pocket_option (vendored PocketOptionApi @ 5b7418a)

## Checklist

| Check | Result | Detail |
|---|---|---|
| PO_SSID loaded from environment | ✅ PASS | value never printed |
| PocketOptionApi importable (vendored) | ✅ PASS |  |
| Provider initialized | ✅ PASS |  |
| Connection succeeds | ✅ PASS |  |
| EURUSD_otc is available | ✅ PASS | payout=77% |
| Subscription succeeds | ✅ PASS | EURUSD_otc |
| Live ticks arrive | ✅ PASS | 1165 ticks |
| Timestamps advance | ✅ PASS | 1164 advances |
| Price changes detected | ✅ PASS | 854 changes |
| Reconnection works | ✅ PASS | forced close mid-run |
| Ticks persisted (parquet round-trip) | ✅ PASS | 1165 rows read back / 1165 captured |
| No order methods called | ✅ PASS | order state untouched |
| No credentials in logs | ✅ PASS | scanned validate_live_otc.log; clean |

## Metrics

- tick_count: **1166**
- first_timestamp: 2026-08-29 16:34:37 UTC
- last_timestamp: 2026-08-29 16:44:37 UTC
- min_price: 1.16675
- max_price: 1.16767
- price_changes: 855
- average_tick_interval: 0.515 s
- latency est. (relative to synced server clock): mean -0 ms, p50 2 ms, p95 32 ms
- data_gaps (> 5s): 1, largest 32.6s
- ticks_persisted: 1166 rows in 58 parquet part file(s)
  - note: one gap is expected from the forced reconnection test

## Connection events

- 2026-08-29 16:32:40 UTC — connecting
- 2026-08-29 16:32:42 UTC — connected
- 2026-08-29 16:32:42 UTC — subscribed EURUSD_otc
- 2026-08-29 16:38:42 UTC — forced_disconnect_test
- 2026-08-29 16:38:57 UTC — stale_resubscribe no ticks 15s
- 2026-08-29 16:38:57 UTC — resubscribe_failed EURUSD_otc
- 2026-08-29 16:39:12 UTC — stale_rebuild no ticks 30s despite connected flag
- 2026-08-29 16:39:12 UTC — connecting
- 2026-08-29 16:39:14 UTC — connected
- 2026-08-29 16:39:14 UTC — resubscribed EURUSD_otc
- 2026-08-29 16:39:14 UTC — rebuilt
- 2026-08-29 16:42:43 UTC — disconnected_by_user

## Errors

- none

## Notes

- Latency is measured against the provider's synced-server-clock estimate;
  it is a relative measure, not absolute one-way latency.
- Credential-leak scan checked 3 secret fragment(s)
  against the run log; the values themselves never appear in this report.
- No order-execution method was invoked at any point (see also
  tests/test_no_order_execution.py).
