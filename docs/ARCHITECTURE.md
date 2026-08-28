# AI AURA — System Architecture (Phase 0 design)

> This architecture is **source-agnostic by design**: every component downstream of the
> Data Source Adapter is independent of where market data comes from. This is deliberate,
> because the single biggest open question of the project (see
> [DATA_SOURCE_RESEARCH.md](DATA_SOURCE_RESEARCH.md)) is *which* data can legitimately
> feed it. Nothing in this document asserts that a Pocket Option data feed exists.

## 1. Component overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA PLANE                                    │
│                                                                      │
│  [Data Source Adapter(s)]   ← pluggable; one adapter per provider    │
│          │                                                           │
│      [Collector]            ← connection mgmt, reconnect, sequencing │
│          │                                                           │
│      [Normalizer]           ← canonical record shape, UTC, precision │
│          │                                                           │
│      [Validator]            ← dup/gap/order/staleness/corruption     │
│          │            └────→ data_quality_events                     │
│      [Raw Store]            ← immutable, append-only                 │
│          │                                                           │
│      [Candle Builder]       ← 1s/5s/…/15m bars, versioned rules      │
└──────────┼──────────────────────────────────────────────────────────┘
           │
┌──────────┼──────────────────────────────────────────────────────────┐
│          ▼             RESEARCH / MODEL PLANE                        │
│      [Feature Engine]       ← versioned, leakage-tested              │
│          │                                                           │
│   ┌──────┴────────┐                                                  │
│   ▼               ▼                                                  │
│ [Strategy      [ML Models]  ← each: direction + prob + version       │
│  Modules]         │                                                  │
│   └──────┬────────┘                                                  │
│          ▼                                                           │
│    [Regime Engine]──┐                                                │
│    [Similarity      │                                                │
│     Engine]─────────┤                                                │
│          ▼          ▼                                                │
│      [Meta Model / Ensemble]  → BUY | SELL + internal telemetry      │
│          │                                                           │
│   [Backtester]  [Walk-forward Harness]  [Experiment Tracker]         │
│   [Champion/Challenger Registry]  [Drift Monitor]                    │
└──────────┼──────────────────────────────────────────────────────────┘
           │
┌──────────┼──────────────────────────────────────────────────────────┐
│          ▼               SERVING PLANE                               │
│      [Signal API]  FastAPI: /api/signals/analyze, /api/feedback      │
│          │         auth · rate limit · validation · audit log        │
│          ▼                                                           │
│      [PWA]  Next.js/TS: pick asset → pick expiry → ANALYZE →         │
│             BUY/SELL → WIN/LOSS feedback → history/analytics         │
│                                                                      │
│      [Learning Loop]  post-trade analysis → batched, gated updates   │
│      [Admin Dashboard]  data health, drift, model performance        │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. Key design rules

1. **Reproducibility.** Every signal row references `model_version`,
   `feature_version`, `dataset_version`, and the exact data snapshot timestamp. A
   signal must be recomputable from the DB alone.
2. **No lookahead.** Features/strategies/models at time T consume only data ≤ T.
   Enforced by automated leakage tests and a canary-leak strategy the backtester must flag.
3. **Signal engine ≠ risk engine.** Direction prediction is separated from the
   monitoring layer that tracks streaks/drawdown/degradation. No Martingale anywhere.
4. **Raw vs. derived.** Raw ticks are immutable and append-only; candles/features are
   derived, versioned, and rebuildable.
5. **Honest uncertainty.** Internal confidence/probability is stored and displayed as
   an estimate with sample sizes and intervals — never as a guarantee.
6. **Expiry realism.** An expiry is only exposed in the UI if the underlying data
   granularity can settle it reliably (e.g., 3–30s expiries require tick/1s data with
   verified feed latency; 1m+ can settle on 1s/1m bars).

## 3. Technology choices (Phase 0 defaults)

| Layer | Choice | Rationale / environment note |
|---|---|---|
| Backend & research | Python 3.13 + FastAPI | Installed (3.13.5); ML ecosystem |
| Frontend | Next.js + React + TypeScript | Node v24.18 installed |
| Database | SQLAlchemy + Alembic; SQLite for dev, PostgreSQL for prod | **Docker/PostgreSQL not installed** on the dev machine; SQLite keeps early research local, migrations stay Postgres-compatible, CI runs against both |
| Cache/queues | None initially; Redis only when a measured need appears | Redis not installed; avoid speculative infra |
| ML | scikit-learn + LightGBM first; deep learning only if justified | Per project rules |
| Testing | pytest, hypothesis (property tests), Playwright, Vitest | |
| Packaging | Docker later (Phase 13); not required for research phases | Docker not installed |

## 4. Open dependency

Everything in the Data Plane is **parameterized by the Phase 0 data-source decision**
recorded in [PHASE0_REPORT.md](PHASE0_REPORT.md). Until a legitimate source is chosen,
no adapter is implemented — a stub adapter against fake data is prohibited by project
rule 1.2.
