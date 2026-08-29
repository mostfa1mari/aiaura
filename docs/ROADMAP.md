# Roadmap

Status legend: ✅ done · 🔶 in progress · ⬜ pending · 🔒 blocked on user input

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository audit (`POCKET_OPTION_API_AUDIT.md`) | ✅ |
| 1 | Provider abstraction + Pocket Option implementation | ✅ (live-unverified) |
| 2 | Live OTC stream + monitor | ✅ code · 🔒 needs `PO_SSID` in `.env` to run |
| 30* | First milestone: 10-min live validation (`LIVE_OTC_VALIDATION.md`) | 🔒 needs `PO_SSID` |
| 3 | Raw tick storage (Parquet) | ✅ (validated offline; live soak pending) |
| 4 | Data quality layer + `DATA_QUALITY.md` | ✅ (offline-tested) |
| 5 | Candle engine (1s…15m from ticks; forming candles marked) | ✅ (offline-tested) |
| 6 | Prediction target definition + reference-price methodology | ⬜ |
| 7 | Feature engine (trend/momentum/volatility/structure/MTF/micro) | ⬜ |
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

## Immediate next step (owner action — credential only)

Live validation needs the session credential, which only the owner can
capture (it is their browser login; it must never pass through chat, and the
build directive assigns this step to the owner). Put `PO_SSID` into `.env`
(instructions in `.env.example`), then:

```
.venv/Scripts/python scripts/validate_live_otc.py --minutes 10
```

Everything buildable and testable **without** the live feed is being done
autonomously in the meantime.
