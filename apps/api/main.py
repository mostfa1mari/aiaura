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
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.feature_engine import compute_features
from services.latency import assess as assess_latency
from services.learning_engine.auto import AutoLearner
from services.learning_engine.dataset import build_dataset
from services.learning_engine.registry import ModelRegistry
from services.market_data.pocket_option_provider import PocketOptionMarketDataProvider
from services.similarity import HistoricalSimilarity
from services.strategies import ensemble as strategy_ensemble
from services.market_data.security import MissingCredentialError, load_ssid, setup_logging
from services.market_data.storage import TickStore
from services.signal_engine import BASELINE_VERSION, generate_signal
from services.signal_engine.ml_predictor import MLPredictor
from services.signal_engine.store import make_store

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
    store: Optional[object] = None       # SqliteSignalStore | PostgresSignalStore
    tick_store: Optional[TickStore] = None
    ml_predictor: Optional[MLPredictor] = None
    model_record: Optional[object] = None
    auto_learner: Optional[AutoLearner] = None
    subscribed: set = set()
    startup_error: Optional[str] = None


state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Store is always available (Postgres/Supabase if DATABASE_URL set, else
    # local SQLite). Provider may fail (missing/expired SSID); keep the API up
    # and report status so the UI can show a clear message.
    state.store = make_store(PROJECT_ROOT / "data" / "aiaura.db")
    state.tick_store = TickStore(PROJECT_ROOT / "data" / "raw" / "ticks")

    # Use a trained champion model if one exists; otherwise the baseline.
    try:
        registry = ModelRegistry(PROJECT_ROOT / "models")
        champ = registry.load_champion()
        if champ is not None:
            rec = registry.champion_record()
            state.ml_predictor = MLPredictor(champ, rec.version)
            state.model_record = rec
            logger.info("champion model loaded: %s", rec.version)
    except Exception as exc:
        logger.warning("no champion model loaded: %s", exc)
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
        # Automatic self-learning: retrains in the background and hot-swaps a
        # promoted champion. Disable with AIAURA_AUTOLEARN=0.
        if (os.environ.get("AIAURA_AUTOLEARN", "1") or "1") != "0":
            def _on_promote(version: str):
                try:
                    reg = ModelRegistry(PROJECT_ROOT / "models")
                    model = reg.load_champion()
                    rec = reg.champion_record()
                    if model is not None and rec is not None:
                        state.ml_predictor = MLPredictor(model, rec.version)
                        state.model_record = rec
                        logger.info("hot-swapped champion -> %s", rec.version)
                except Exception:
                    logger.warning("champion hot-swap failed", exc_info=True)

            from services.learning_engine.auto import AutoLearnConfig
            cfg = AutoLearnConfig()
            if os.environ.get("AIAURA_AUTOLEARN_WARMUP"):
                cfg.warmup_delay_s = float(os.environ["AIAURA_AUTOLEARN_WARMUP"])
            if os.environ.get("AIAURA_AUTOLEARN_INTERVAL"):
                cfg.interval_s = float(os.environ["AIAURA_AUTOLEARN_INTERVAL"])
            if os.environ.get("AIAURA_AUTOLEARN_LOSS_TRIGGER"):
                cfg.loss_trigger = int(os.environ["AIAURA_AUTOLEARN_LOSS_TRIGGER"])
            state.auto_learner = AutoLearner(
                provider, state.store, PROJECT_ROOT / "models", _on_promote, cfg)
            state.auto_learner.start()
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
        if state.auto_learner is not None:
            try:
                state.auto_learner.stop()
            except Exception:
                pass
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

# CORS so a PWA hosted on another origin (e.g. Vercel) can call this worker.
# Auth is by bearer token (not cookies), so a permissive default origin is safe;
# restrict with ALLOWED_ORIGINS="https://you.vercel.app,https://..." if desired.
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(authorization: str = Header(default="")):
    """Enforce AIAURA_TOKEN when set. No-op when unset (LAN/dev). /api/health
    stays open so a client can probe reachability before it has the token."""
    token = (os.environ.get("AIAURA_TOKEN") or "").strip()
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid or missing access token")


class AnalyzeRequest(BaseModel):
    asset: str
    expiry_s: int


class SubscribeRequest(BaseModel):
    asset: str


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
    auth_required = bool((os.environ.get("AIAURA_TOKEN") or "").strip())
    if state.provider is None:
        return {"connected": False, "auth_required": auth_required,
                "error": state.startup_error or "provider not started"}
    h = state.provider.health_check()
    return {
        "connected": h.connected,
        "auth_required": auth_required,
        "status": h.status,
        "time_synced": h.time_synced,
        "ticks_received": h.ticks_received,
        "subscribed": list(h.subscribed_assets),
        "reconnects": h.reconnect_count,
        "detail": h.detail,
        "model_version": BASELINE_VERSION,
        "store": getattr(state.store, "backend", "unknown"),
    }


@app.get("/api/expiries")
def expiries():
    return {"expiries": EXPIRIES}


@app.get("/api/assets", dependencies=[Depends(require_token)])
def assets():
    provider = _require_provider()
    otc = provider.get_otc_assets()
    items = [
        {"symbol": s, "name": info.name, "payout": info.payout}
        for s, info in sorted(otc.items())
    ]
    return {"assets": items, "count": len(items)}


@app.post("/api/subscribe", dependencies=[Depends(require_token)])
def subscribe(req: SubscribeRequest):
    """Warm an asset's stream so a subsequent /analyze is instant. Idempotent."""
    provider = _require_provider()
    if req.asset not in state.subscribed:
        try:
            provider.subscribe(req.asset)
            state.subscribed.add(req.asset)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"cannot subscribe to {req.asset}: {exc}")
    return {"ok": True, "subscribed": sorted(state.subscribed)}


@app.post("/api/analyze", dependencies=[Depends(require_token)])
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

    if state.ml_predictor is not None:
        result = state.ml_predictor.predict(candles, timeframe)
    else:
        result = generate_signal(candles, timeframe)

    tick = provider.get_latest_tick(asset)
    entry_price = tick.price if tick else None
    market_ts = tick.source_timestamp if tick else None
    latency_ms = (time.time() - t0) * 1000.0

    info = provider.get_assets().get(asset)
    payout = info.payout if info else None

    # Secondary, advisory context: strategy-ensemble agreement, historical
    # similarity, and short-horizon latency viability. All no-look-ahead.
    strategies = similarity = latency_viability = None
    try:
        closed = [c for c in candles if c.complete]
        fv = compute_features(closed, timeframe_s=timeframe)
        ens = strategy_ensemble(fv.values)
        strategies = {"signal": ens.signal, "agreement": round(ens.agreement, 3),
                      "contributors": ens.contributors}
        window = closed[-400:]
        rows, names = build_dataset(window, horizon_s=float(req.expiry_s), warmup=50)
        if len(rows) >= 40:
            sim = HistoricalSimilarity([(r.timestamp, r.features, r.label) for r in rows])
            sr = sim.query(fv.as_row(names), k=30, as_of=fv.at_timestamp)
            similarity = {"directional_rate": round(sr.directional_rate, 3),
                          "leans": sr.leans, "n_neighbors": sr.n_neighbors,
                          "confident": sr.confident}
        tick_age = max(0.0, tick.latency_ms) if tick else 0.0
        lv = assess_latency(latency_ms, float(req.expiry_s), tick_age_ms=tick_age)
        latency_viability = {"verdict": lv.verdict, "fraction": lv.fraction_of_horizon}
    except Exception:
        logger.debug("secondary analysis failed", exc_info=True)

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
        "payout": payout,
        "market_ts": market_ts,
        "prediction_latency_ms": round(latency_ms, 1),
        "model_version": result.model_version,
        "note": result.note,
        "sub_signals": context["sub_signals"],
        "strategies": strategies,
        "historical_similarity": similarity,
        "latency_viability": latency_viability,
        "created_at": time.time(),
    }


@app.post("/api/feedback", dependencies=[Depends(require_token)])
def feedback(req: FeedbackRequest):
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    try:
        ok = state.store.record_result(req.signal_id, req.result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="unknown signal_id or already settled")
    # Deep post-trade analysis on a loss: record WHY it lost (never retrain on a
    # single trade — the batched auto-learner uses the accumulated evidence).
    if req.result.upper() == "LOSS":
        try:
            from services.learning_engine.post_trade import analyze_loss
            pred = state.store.get_prediction(req.signal_id)
            if pred:
                state.store.set_analysis(req.signal_id, analyze_loss(pred))
        except Exception:
            logger.debug("loss analysis failed", exc_info=True)
    return {"ok": True}


@app.get("/api/losses", dependencies=[Depends(require_token)])
def losses():
    from services.learning_engine.post_trade import aggregate_losses

    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    return aggregate_losses(state.store.losses_for_analysis(limit=500))


@app.get("/api/stats", dependencies=[Depends(require_token)])
def stats():
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    return state.store.stats()


@app.post("/api/reset", dependencies=[Depends(require_token)])
def reset_stats():
    """UI-ONLY reset: zero the visible counters but KEEP all data — the model
    keeps learning from every recorded prediction and outcome."""
    if state.store is None:
        raise HTTPException(status_code=503, detail="store unavailable")
    hidden = state.store.reset_display()
    return {"ok": True, "hidden": hidden,
            "note": "Counters reset for display only; all data is kept and still used for learning."}


@app.get("/api/model", dependencies=[Depends(require_token)])
def model_info():
    from dataclasses import asdict

    from services.learning_engine.self_learning import detect_drift

    active = state.ml_predictor is not None
    rec = state.model_record
    champion = asdict(rec) if rec is not None else None
    drift = None
    if rec is not None and state.store is not None:
        s = state.store.stats()
        baseline_wr = (rec.metrics or {}).get("oos_win_rate")
        if baseline_wr and s.get("settled", 0) >= 30:
            drift = detect_drift(s["wins"], s["settled"], float(baseline_wr))
    records = []
    try:
        records = [asdict(r) for r in ModelRegistry(PROJECT_ROOT / "models").records()]
    except Exception:
        pass
    al = state.auto_learner
    auto = None
    if al is not None:
        auto = {"running": True, "cycles": al.cycles,
                "last_train_at": al.last_train_at or None,
                "last_result": al.last_result}
    return {
        "using_ml": active,
        "active_model": rec.version if rec else BASELINE_VERSION,
        "champion": champion,
        "records": records,
        "drift": drift,
        "auto_learner": auto,
        "note": ("A trained champion model is active." if active else
                 "No trained model yet — the app uses the transparent baseline. "
                 "Auto-learning is retraining in the background and will deploy a "
                 "champion automatically only once one shows a real, significant "
                 "edge on held-out data (it never deploys noise)."),
    }


@app.get("/api/recent", dependencies=[Depends(require_token)])
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


@app.get("/admin")
def admin():
    page = WEB_DIR / "admin.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(status_code=404, detail="dashboard not built")


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
