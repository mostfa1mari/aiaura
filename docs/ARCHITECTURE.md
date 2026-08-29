# Architecture

## Principle

AI AURA is a **read-only signal and research platform** for Pocket Option
OTC. It never places trades. The user executes signals manually and reports
WIN/LOSS back; learning happens in controlled batches with validation.

## Layering (target)

```
Pocket Option OTC
        ↓
PocketOptionApi (vendored, audited)          ← only services/market_data touches this
        ↓
PocketOptionMarketDataProvider               ← implements MarketDataProvider
        ↓
Canonical market data (UTC ticks/candles)    ← the ONLY contract downstream code sees
        ↓
Candle Engine → Feature Engine → Strategies → ML Models → Meta Model
        ↓
BUY / SELL signal  →  PWA  →  user executes manually  →  WIN/LOSS feedback
        ↓
Learning pipeline (batched, walk-forward validated, champion/challenger)
```

Swapping the data source later = one new `MarketDataProvider` subclass.

## Repository layout

```
aiaura/
  apps/                    # (upcoming) web PWA + api service
  services/
    market_data/           # provider abstraction, PO implementation, storage
      vendor/              # pinned pocketoptionapi snapshot (audited, patched)
  data/                    # raw/derived market data — gitignored, local only
  docs/                    # audits, schemas, validation reports
  scripts/                 # live_monitor, validate_live_otc, ops tooling
  tests/                   # offline tests incl. the no-order-execution guard
```

## Key design decisions

1. **Vendored provider library** — pinned audited snapshot, two marked
   patches (TLS verification, stream batching). See `VENDOR_NOTES.md`.
2. **Canonical UTC everywhere** — provider-native clocks (PO wire = UTC+2)
   are normalized at the boundary; the raw wire value is retained per tick.
3. **Push-based ingestion** — provider tees the library's tick handler; no
   polling races, no 500-tick ring-buffer cap; listeners fan out (storage,
   monitor, future engines).
4. **Supervised connection** — re-subscribe on reconnect, client rebuild on
   thread death, terminal stop on auth failure.
5. **Raw vs derived separation** — raw ticks are immutable history; quality
   flags, candles, and features are derived layers with their own versions.
6. **Read-only enforced by tests** — the order-execution blocklist is a
   failing test away from any regression.
