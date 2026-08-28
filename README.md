# AI AURA

A research-grade signal-generation platform: pick an asset, pick an expiry, press
**ANALYZE**, get one direction (**BUY** / **SELL**), execute manually, then report
**WIN** / **LOSS** so the system learns. Manual execution only — no auto-trading, no
Martingale, no guaranteed-profit claims.

> **Status: Phase 0 (Discovery) complete — project gated on a data-source decision.**
> See **[docs/PHASE0_REPORT.md](docs/PHASE0_REPORT.md)**.

## The honest headline

Phase 0 research (adversarially verified) found that Pocket Option's OTC prices are a
**synthetic, broker-generated feed with no official API and no legitimate/licensed data
path** — the only access is a ToS-sanctioned, fragile reverse-engineered scrape from an
**unregulated** counterparty offering a product **banned for retail in the EU/UK/AU/CA**.
Building the literal "predict Pocket Option" product would require fabricating data,
defeating a CAPTCHA/anti-bot gate, or violating the platform's terms — none of which
this project will do.

The **full engineering** (data pipeline → features → leakage-tested backtesting → ML →
ensemble → self-learning → PWA → monitoring) is real and buildable. What must be decided
is **which real, legitimate market** it predicts. Four options are laid out in the
[Phase 0 report](docs/PHASE0_REPORT.md); **Option A** (rebuild on real, cleanly-licensed
crypto/FX data) is recommended.

## Principles (non-negotiable)

No fake data · no fabricated APIs · no guaranteed profits · no Martingale · manual
execution only · no lookahead/leakage · every prediction reproducible and versioned ·
honest reporting including negative results.

## Documentation

- [ROADMAP.md](docs/ROADMAP.md) — 16 phases, each with exit criteria and quality gates
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — source-agnostic system design
- [DATA_SOURCE_RESEARCH.md](docs/DATA_SOURCE_RESEARCH.md) — Phase 0 findings with sources
- [PHASE0_REPORT.md](docs/PHASE0_REPORT.md) — quality gate & the decision required

## Environment

Node v24.18 · Python 3.13 · Git 2.55 installed. PostgreSQL/Docker/Redis not yet
installed — dev uses SQLite (Postgres-compatible via SQLAlchemy) until Phase 13.

## Legal / risk note

Retail binary options are banned or restricted in many jurisdictions and Pocket Option
is unregulated and on multiple financial-regulator warning lists. Nothing here is
financial advice or a solicitation to trade. This repository is a software-engineering
and quantitative-research project.
