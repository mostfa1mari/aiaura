# AI AURA — Project Roadmap

> Status legend: `NOT STARTED` · `IN PROGRESS` · `PASS` · `FAIL` · `BLOCKED`
>
> A phase may not begin until the previous phase's exit criteria are met.
> No phase may be marked PASS without evidence recorded in its completion report.

---

## Global constraint (read first)

Every phase below that touches market data is conditioned on the **Phase 0 data-source
decision**. Per project rules ([Section 1.6 / 4 of the master brief](PHASE0_REPORT.md)):
no fabricated data, no invented APIs, no ToS-violating access. If no legitimate source
exists for a given instrument class, work on that instrument class stops rather than
proceeding on fake or misrepresentative data.

---

## PHASE 0 — Discovery & Feasibility — `BLOCKED (decision required)`

**Objective.** Establish what is actually buildable: audit the environment, determine
whether Pocket Option OTC price data is legitimately accessible, and design the
architecture around verified facts only.

**Inputs.** Empty repository; developer machine; public web sources.

**Outputs.** Environment audit, `DATA_SOURCE_RESEARCH.md`, `ARCHITECTURE.md`, this
roadmap, `PHASE0_REPORT.md` with a quality-gate verdict.

**Tasks.**
- [x] Inspect repository, OS, toolchain (Node, Python, Git, Docker, DB engines).
- [x] Research Pocket Option official API / data export availability (multi-agent, adversarially verified).
- [x] Research Pocket Option Terms of Service constraints on automated access.
- [x] Research what OTC quotes actually are (who generates them, whether any vendor redistributes them).
- [x] Survey the unofficial API ecosystem (documentation only — not a permitted path).
- [x] Research regulatory status of the platform and of retail binary options generally.
- [x] Research legitimate alternative data sources (licensed FX/crypto feeds).
- [x] Design source-agnostic architecture.
- [x] Write Phase 0 report with honest feasibility verdict.

**Acceptance criteria.** Every factual claim in `DATA_SOURCE_RESEARCH.md` carries a
source URL or is explicitly marked unknown; no endpoint or capability is asserted
without evidence; blockers are stated, not papered over.

**Tests.** N/A (research phase). Verification = adversarial refutation agents on the
three pivotal claims (see DATA_SOURCE_RESEARCH.md §Verification).

**Risks.** The central risk materialized: see exit criteria.

**Deliverables.** `/docs/DATA_SOURCE_RESEARCH.md`, `/docs/ARCHITECTURE.md`,
`/docs/ROADMAP.md`, `/docs/PHASE0_REPORT.md`, initialized Git repo.

**Exit criteria.** A data-source decision is made by the project owner from the
options in `PHASE0_REPORT.md §Decision required`. **Until then, Phases 1+ do not start.**

---

## PHASE 1 — Data Acquisition — `NOT STARTED`

**Objective.** Reliable, continuous, quality-checked ingestion of real market data from
the source(s) chosen at the Phase 0 gate.

**Inputs.** Phase 0 data-source decision; credentials/keys for the chosen provider(s).

**Outputs.** Running collector service; raw immutable tick/candle store; data-quality
reports.

**Tasks.**
- Implement `Collector → Normalizer → Validator → Raw Storage → Candle Builder` pipeline.
- Per-record metadata: asset, source, source timestamp, collection timestamp, sequence, precision, quality status.
- Duplicate / gap / out-of-order / stale-feed / corrupted-record detection.
- Reconnect and backfill handling; structured logging.
- Daily data-quality report generation.

**Acceptance criteria.** ≥99% capture rate over a 7-day continuous run (measured, not
assumed); every anomaly logged as a `data_quality_event`; raw data immutable and
separated from derived data.

**Tests.** Unit tests for normalizer/validator with synthetic fixtures (labeled
synthetic); integration test against provider sandbox; chaos tests (kill connection,
inject duplicates, reorder messages).

**Risks.** Provider rate limits; clock skew; silent feed degradation.

**Deliverables.** `services/collector/`, quality-report generator, 7-day capture evidence.

**Exit criteria.** 7-day continuous capture meets acceptance criteria; quality gate report committed.

---

## PHASE 2 — Data Infrastructure — `NOT STARTED`

**Objective.** Durable, reproducible storage with schema-versioned records.

**Inputs.** Phase 1 pipeline; DB engine decision (note: no Docker/PostgreSQL on the dev
machine yet — install PostgreSQL, or dev on SQLite with a SQLAlchemy layer that is
schema-compatible with Postgres for deployment).

**Outputs.** Migrations for: users, assets, ticks, candles, features, signals, trades,
trade_results, model_versions, learning_events, backtests, market_regimes,
strategy_scores, performance_metrics, data_quality_events.

**Tasks.** Schema design; migration tooling (Alembic); retention policy; backup script;
reproducibility keys (every prediction references model_version, feature_version,
data snapshot, timestamp).

**Acceptance criteria.** A stored signal can be fully reproduced from DB contents alone.

**Tests.** Migration up/down tests; reproducibility round-trip test; constraint tests.

**Risks.** SQLite→Postgres drift (mitigate: run CI against both); disk growth on tick data.

**Exit criteria.** Reproducibility test passes; all tables migrated with tests green.

---

## PHASE 3 — Market Feature Engine — `NOT STARTED`

**Objective.** Modular, versioned, leak-proof feature computation.

**Inputs.** Phase 2 candle/tick store.

**Outputs.** Feature registry with `feature_version`; feature matrices keyed by (asset, timeframe, timestamp).

**Tasks.** Trend (EMA/SMA/slopes/structure), momentum (RSI/MACD/Stoch/ROC), volatility
(ATR/BB width/realized vol/regime), trend strength (ADX/DM), price action (bodies,
wicks, ranges, breakout/rejection structures), market structure (S/R, swings, ranges),
multi-timeframe derivation where data validity allows.

**Acceptance criteria.** Every feature computed at time T uses only data ≤ T
(enforced by automated leakage tests, not convention).

**Tests.** Golden-value unit tests per indicator (cross-checked against a reference
implementation); **leakage tests**: recompute features with future rows removed and
assert bit-identical values; property tests on NaN/warm-up handling.

**Risks.** Off-by-one candle indexing (the classic leak); warm-up window mishandling.

**Exit criteria.** 100% of features pass the leakage suite.

---

## PHASE 4 — Baseline Quantitative Strategies — `NOT STARTED`

**Objective.** Independent, testable rule-based strategy modules producing
(direction, score, reason, feature contributions, version).

**Tasks.** Trend-following, momentum, mean reversion, breakout, S/R, reversal,
volatility expansion/contraction, candlestick patterns, MTF confirmation. No strategy
is presumed profitable; all are hypotheses.

**Acceptance criteria.** Every strategy runs on historical data without lookahead;
outputs are deterministic given (data snapshot, version).

**Tests.** Unit tests per strategy on constructed scenarios; determinism tests; leakage tests.

**Exit criteria.** All strategies pass tests and produce evaluable signals over ≥6 months of historical data (or maximum legitimately available).

---

## PHASE 5 — Historical Backtesting — `NOT STARTED`

**Objective.** Event-driven backtester that replays time strictly forward, evaluating
per (asset × expiry × strategy × regime × session × volatility state).

**Tasks.** Event loop; expiry settlement logic per supported expiry (only expiries the
data granularity can actually settle — sub-minute expiries require tick or 1s data);
payout-adjusted expectancy; metrics: trades, W/L, win rate ±CI (Wilson), profit factor,
max drawdown, max losing streak, EV, stability across windows; statistical
significance vs. the 50% + payout-hurdle null.

**Acceptance criteria.** Win rates never reported without sample size and CI; a
deliberately-leaky test strategy is *caught* by the harness (canary test).

**Tests.** Settlement unit tests; canary leak strategy must be flagged; replay determinism.

**Exit criteria.** Canary tests pass; baseline strategies evaluated and honestly reported (including negative results).

---

## PHASE 6 — Machine Learning Research — `NOT STARTED`

**Objective.** Benchmarked model families beating strong baselines on
chronological out-of-sample data — or an honest report that they don't.

**Tasks.** Logistic regression → random forest → gradient boosting (LightGBM/XGBoost);
sequence/neural models only if data volume and prior results justify; chronological
train/val/test + walk-forward; probability calibration; experiment records in `/experiments/`.

**Acceptance criteria.** Every experiment records hypothesis, dataset_version,
feature_version, params, periods, results, conclusion. Failed experiments are kept.

**Exit criteria.** At least one model family evaluated with walk-forward evidence; verdict (edge / no edge / inconclusive) documented.

---

## PHASE 7 — Ensemble / Meta Model — `NOT STARTED`

**Objective.** Meta-model combining strategy outputs, model outputs, regime,
volatility, MTF context, historical-similarity stats, and data quality — learned, not
averaged.

**Exit criteria.** Meta-model ≥ best single model on out-of-sample walk-forward, or the simpler champion is retained and the result documented.

---

## PHASE 8 — Self-Learning System — `NOT STARTED`

**Objective.** Post-trade analysis and batched, statistically-gated learning from
reported WIN/LOSS outcomes. Never update production from a single trade; minimum
batch ~100 comparable observations with significance testing. Champion/challenger
promotion rules; full model-version registry; rollback.

**Exit criteria.** Simulated feedback replay shows correct gating (no premature updates); promotion/rollback tested.

---

## PHASE 9 — Signal API — `NOT STARTED`

**Objective.** Secure FastAPI service: `POST /api/signals/analyze` → BUY/SELL +
confidence, agreement, regime, data-quality, model_version, signal_id. Auth, rate
limiting, input validation, no secrets in frontend, structured audit logging.

**Exit criteria.** Integration + adversarial tests pass (invalid asset/expiry, timeout, degraded data).

---

## PHASE 10 — PWA — `NOT STARTED`

**Objective.** Mobile-first Next.js/TypeScript PWA: asset picker (dynamic registry),
expiry picker (only reliably-evaluable expiries), ANALYZE → one large BUY/SELL,
secondary context panel, WIN/LOSS feedback, history, analytics.

**Exit criteria.** E2E flow (select → analyze → signal → report result → persisted → learning event) green in Playwright.

---

## PHASE 11 — Paper Trading — `NOT STARTED`

**Objective.** Forward-testing on live data without money: log signals at prediction
time, settle against subsequent data, accumulate genuinely out-of-sample evidence.

**Exit criteria.** ≥500 paper trades across regimes with pre-registered evaluation plan; honest report with CIs.

---

## PHASE 12 — Validation & Stress Testing — `NOT STARTED`

Adversarial suite: missing/corrupt/duplicate/out-of-order data, volatility shocks,
model failure, DB failure, API timeouts. Exit: all failure modes degrade safely and visibly.

---

## PHASE 13 — Production Hardening — `NOT STARTED`

Security review, secret management, deployment recipe, backup/restore drill.

---

## PHASE 14 — Monitoring & MLOps — `NOT STARTED`

Drift detection (feature/volatility/regime/performance/prediction-distribution),
learning-event workflow, admin research dashboard, alerting.

---

## PHASE 15 — Continuous Research — `NOT STARTED`

Rolling evaluation (50/100/250/500/1000-trade windows with uncertainty), loss & win
analysis discipline, model memory, experiment cadence, research log.

---

## Final production criteria

The project is complete only when **all** of: data pipeline runs with measured quality;
historical data exists; backtesting + leakage tests pass; baselines and ML models
benchmarked; ensemble + learning system + champion/challenger + versioning operate;
PWA + feedback work end-to-end; tests pass; docs current; monitoring live; paper-trading
validation completed **and honestly reported** — including the possibility that the
honest result is "no exploitable edge exists."
