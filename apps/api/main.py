"""AI AURA API — read-only signal service + PWA host.

Wires the live Pocket Option provider -> candle history -> baseline signal
engine -> prediction store, and serves the PWA. NEVER places trades.

Run:
    .venv/Scripts/python -m uvicorn apps.api.main:app --port 8000
or:
    .venv/Scripts/python apps/api/main.py
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.market_data.security import MissingCredentialError, load_ssid, setup_logging
from services.market_data.storage import TickStore
from services.signal_engine import BASELINE_VERSION, generate_signal
from services.signal_engine.store import SignalStore

logger = logging.getLogger("aiaura.api")

WEB_DIR = PROJECT_ROOT / "apps" / "web"

# Offered expiries (seconds) and the candle timeframe used to analyze each.
EXPIRY_TIMEFRAME = {
    5: 5,
    15: 5,
    30: 10,
    60: 15,
    180: 30,
    300: 60,
    900: 60,
}
EXPIRIES = sorted(EXPIRY_TIMEFRAME)
DEFAULT_ASSET = "EURUSD_otc"


class AppState:
    provider: Optional[PocketOptionMarketDataProvider] = None
    store: Optional[SignalStore] = None
    tick_store: Optional[TickStore] = None
    subscribed: set = set()
    startup_error: Optional[str] = None


state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Store is always available (local). Provider may fail (missing/expired SSID);
    # keep the API up and report status so the UI can show a clear message.
    state.store = SignalStore(PROJECT_ROOT / "data" / "aiaura.db")
    state.tick_store = TickStore(PROJECT_ROOT / "data" / "raw" / "ticks")
    try:
        ssid = load_ssid()
        setup_logging(ssid, level=logging.INFO,
                      log_file=PROJECT_ROOT / "logs" / "api.log", console=False)
        provider = PocketOptionMarketDataProvider(ssid)
        provider.add_tick_listener(state.tick_store.append)  # keep collecting while serving
        provider.connect()
        state.provider = provider
        # Subscribe a default asset immediately so ticks flow and the
        # connection stays warm (an idle, unsubscribed socket receives nothing).
        try:
            provider.subscribe(DEFAULT_ASSET)
            state.subscribed.add(DEFAULT_ASSET)
        except Exception as exc:
            logger.warning("default subscribe failed: %s", exc)
        logger.info("provider connected")
    except MissingCredentialError as exc:
        state.startup_error = str(exc).splitlines()[0]
        logger.error("provider not started: %s", state.startup_error)
    except Exception as exc:
        state.startup_error = f"provider connect failed: {exc}"
        logger.error(state.startup_error)
    try:
        yield
    finally:
        if state.provider is not None:
            try:
                state.provider.disconnect()
            except Exception:
                pass
        if state.tick_store is not None:
            state.tick_store.close()
        if state.store is not None:
            state.store.close()


app = FastAPI(title="AI AURA", version=BASELINE_VERSION, lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    asset: str
    expiry_s: int


class FeedbackRequest(BaseModel):
    signal_id: str
    result: str  # WIN | LOSS


def _require_provider() -> PocketOptionMarketDataProvider:
    if state.provider is None or not state.provider.is_connected():
        detail = state.startup_error or (
            state.provider.health_check().detail if state.provider else "provider not connected"
        )
        raise HTTPException(status_code=503, detail=f"Market data unavailable: {detail}")
    return state.provider


@app.get("/api/health")
def health():
    if state.provider is None:
        return {"connected": False, "error": state.startup_error or "provider not started"}
    h = state.provider.health_check()
    return {
        "connected": h.connected,
        "status": h.status,
        "time_synced": h.time_synced,
        "ticks_received": h.ticks_received,
        "subscribed": list(h.subscribed_assets),
        "reconnects": h.reconnect_count,
        "detail": h.detail,
        "model_version": BASELINE_VERSION,
    }


@app.get("/api/expiries")
def expiries():
    return {"expiries": EXPIRIES}


@app.get("/api/assets")
def assets():
    provider = _require_provider()
    otc = provider.get_otc_assets()
    items = [
        {"symbol": s, "name": info.name, "payout": info.payout}
        for s, info in sorted(otc.items())
    ]
    return {"assets": items, "count": len(items)}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    provider = _require_provider()
    if req.expiry_s not in EXPIRY_TIMEFRAME:
        raise HTTPException(status_code=400, detail=f"expiry_s must be one of {EXPIRIES}")
    asset = req.asset
    timeframe = EXPIRY_TIMEFRAME[req.expiry_s]

    t0 = time.time()
    # Ensure subscribed and that the stream tz offset is established.
    if asset not in state.subscribed:
        try:
            provider.subscribe(asset)
            state.subscribed.add(asset)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"cannot subscribe to {asset}: {exc}")
    provider.wait_for_first_tick(asset, timeout_s=10.0)

    try:
        candles = provider.get_historical_candles(asset, timeframe, pages=1)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"history unavailable: {exc}")

    result = generate_signal(candles, timeframe)

    tick = provider.get_latest_tick(asset)
    entry_price = tick.price if tick else None
    market_ts = tick.source_timestamp if tick else None
    latency_ms = (time.time() - t0) * 1000.0

    context = {
        "sub_signals": [
            {"name": s.name, "direction": s.direction, "score": round(s.score, 4), "detail": s.detail}
            for s in result.sub_signals
        ],
        "candles_used": result.candles_used,
        "timeframe_s": timeframe,
    }
    signal_id = state.store.record_prediction(
        asset=asset, expiry_s=req.expiry_s, signal=result.signal, score=result.score,
        strength=result.strength, agreement=result.agreement, regime=result.regime,
        data_sufficiency=result.data_sufficiency, entry_price=entry_price,
        market_ts=market_ts, prediction_latency_ms=latency_ms,
        model_version=result.model_version, context=context,
    )

    return {
        "signal_id": signal_id,
        "asset": asset,
        "expiry_s": req.expiry_s,
        "signal": result.signal,
        "strength": round(result.strength, 4),
        "agreement": round(result.agreement, 4),
        "regime": result.regime,
        "data_sufficiency": round(result.data_sufficiency, 3),
        "candles_used": result.candles_used,
        "entry_price": entry_price,
        "market_ts": market_ts,
        "prediction_latency_ms": round(latency_ms, 1),
        "model_version": result.model_version,
        "note": result.note,
        "sub_signals": context["sub_signals"],
        "created_at": time.time(),
    }


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    try:
        ok = state.store.record_result(req.signal_id, req.result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="unknown signal_id or already settled")
    return {"ok": True}


@app.get("/api/stats")
def stats():
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    return state.store.stats()


@app.get("/api/recent")
def recent(limit: int = 50):
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    return {"recent": state.store.recent(limit=min(limit, 200))}


# --- PWA hosting ------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"error": "PWA not built"}, status_code=404)


@app.get("/manifest.webmanifest")
def manifest():
    m = WEB_DIR / "manifest.webmanifest"
    if m.exists():
        return FileResponse(str(m), media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="no manifest")


@app.get("/service-worker.js")
def service_worker():
    sw = WEB_DIR / "service-worker.js"
    if sw.exists():
        return FileResponse(str(sw), media_type="application/javascript")
    raise HTTPException(status_code=404, detail="no service worker")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
