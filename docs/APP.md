# App (PWA + API)

The user-facing product: a mobile-first PWA backed by a FastAPI service that
wires the live provider → candle history → baseline signal → prediction store.
Read-only: it never places trades; the user executes each signal manually.

## Run

```
.venv/Scripts/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Requires a valid `PO_SSID` in `.env`; if it is
missing/expired the API still starts and the UI shows an "offline" pill with a
clear message (recapture the SSID and restart).

## Flow

Select OTC pair → select expiry → **ANALYZE** → **BUY** / **SELL** with signal
strength, model agreement, market regime, data sufficiency, entry price,
latency, model version → you execute manually on Pocket Option → tap **WIN** or
**LOSS**. Every prediction and outcome is stored (`data/aiaura.db`) for
post-trade analysis and future model training (Phase 14). The UI never shows
WAIT; uncertainty is shown as strength/agreement.

## API (`apps/api/main.py`)

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | provider status, tick count, model version |
| `GET /api/assets` | available OTC symbols + live payout |
| `GET /api/expiries` | offered expiries (5s…15m) |
| `POST /api/analyze` `{asset, expiry_s}` | generate + record a BUY/SELL signal |
| `POST /api/feedback` `{signal_id, result}` | record WIN/LOSS |
| `GET /api/stats` | wins/losses/win-rate (settled only), by asset/expiry, max losing streak |
| `GET /api/recent` | recent predictions |
| `/`, `/static/*`, `/manifest.webmanifest`, `/service-worker.js` | PWA |

The API keeps a default subscription (EURUSD_otc) alive so ticks flow and the
socket stays warm, and also persists every incoming tick to `data/raw/ticks/`
(the app collects data while it serves).

## Signal engine (`services/signal_engine/baseline.py`)

A transparent BASELINE ensemble of classic heuristics (EMA trend + slope, RSI,
ROC, price-action body/range) combined into a directional score in [-1,+1].
It is **not a validated edge**, makes **no probability claim**, and is fully
inspectable (`sub_signals` are recorded per prediction). ML models (Phases
10–11) replace the scoring later behind the same `SignalResult` interface, and
the app improves as `data/raw/ticks/` and `data/aiaura.db` accumulate.

## Honesty

- No WAIT in the UI; strength/agreement carry uncertainty.
- Win rate is over user-reported settled signals only, with a permanent
  disclaimer that the sample may be tiny and there is no guarantee.
- No martingale / stake logic anywhere; the app only predicts direction.
- No order-execution code (enforced by `tests/test_no_order_execution.py`,
  which scans `apps/` too).
