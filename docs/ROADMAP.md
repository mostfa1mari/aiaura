# Roadmap

Status legend: ✅ done · 🔶 in progress · ⬜ pending · 🔒 blocked on user input

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository audit (`POCKET_OPTION_API_AUDIT.md`) | ✅ |
| 1 | Provider abstraction + Pocket Option implementation | ✅ (live-verified) |
| 2 | Live OTC stream + monitor | ✅ (live-verified 2026-08-29) |
| 30* | First milestone: 10-min live validation (`LIVE_OTC_VALIDATION.md`) | ✅ **PASSED 2026-08-29** (12/12 checks) |
| 3 | Raw tick storage (Parquet) | ✅ (live-verified: 1166 ticks round-tripped) |
| 4 | Data quality layer + `DATA_QUALITY.md` | ✅ (offline-tested) |
| 5 | Candle engine (1s…15m from ticks; forming candles marked) | ✅ (offline-tested) |
| 6 | Prediction target definition + reference-price methodology | ✅ (offline-tested; `PREDICTION_TARGET.md`) |
| 7 | Feature engine (trend/momentum/volatility/structure/MTF/micro) | 🔶 next |
| 8 | Baseline strategy modules | ⬜ |
| 9 | Event-driven backtester + metrics | ⬜ |
| 10 | ML model families + calibration | ⬜ |
| 11 | Ensemble + meta-model (UI shows BUY/SELL only) | ⬜ |
| 12 | Market regime detection | ⬜ |
| 13 | Historical similarity | ⬜ |
| 14 | Self-learning loop (WIN/LOSS feedback, batched retraining) | ⬜ |
| 15 | Overfitting protection + leakage tests | ⬜ |
| 16 | Champion/challenger + model registry | ⬜ |
| 17 | Drift detection | ⬜ |
| 18 | PWA | ⬜ |
| 19 | Live signal pipeline | ⬜ |
| 20 | Latency measurement (3s/5s viability check) | ⬜ (per-tick latency already recorded) |
| 21 | Full test pyramid | 🔶 (21 offline tests) |
| 22 | Security hardening | 🔶 (redaction, TLS patch, guard tests done; CI secret-scan pending) |
| 23 | Admin/research dashboard | ⬜ |
| 24–26 | Structure, docs, git checkpoints | 🔶 ongoing |
| 27–28 | No martingale, no guarantees | permanent constraints |

\* Phase 30 (per build directive) is the live gate: the 10-minute validation
must pass before any phase is trusted on live data. Phases 4–5 are pure
offline data-processing (fully unit-tested with synthetic ticks, no live
feed, no fabricated market data), built ahead with the owner's explicit
authorization; they will be exercised on real ticks the moment the live gate
passes.

## Milestone status

**Phase 30 PASSED on 2026-08-29** (see `LIVE_OTC_VALIDATION.md`): 10-minute
live soak on `EURUSD_otc`, 1166 ticks, ~0.52 s mean interval, forced-reconnect
recovery verified, parquet round-trip verified, no order calls, no credential
leak. The gate is open — build now continues to Phase 6 onward on live data.

## Next steps (autonomous)

6 → prediction target + reference-price methodology, then 7 (feature engine).
Re-run the live validation any time with:

```
.venv/Scripts/python scripts/validate_live_otc.py --minutes 10
```

Note: the `PO_SSID` session expires when the browser session ends or Pocket
Option rotates it. On auth failure the provider stops (no hammering) and
`health_check().detail` says so — recapture a fresh frame into `.env`.
