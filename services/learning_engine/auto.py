"""Automatic self-learning loop (Phase 14, 16, 17).

Runs in the background inside the API — no manual step. On a schedule it:
  1. pulls fresh candle history for the configured targets,
  2. runs a training cycle (controlled batch, never per-trade),
  3. lets the strict promotion gate decide — a challenger is deployed ONLY if it
     shows a held-out, significant edge and beats the champion,
  4. hot-reloads a promoted champion so the app uses it immediately,
  5. watches live outcomes for drift and retrains sooner when it degrades.

"Automatic" never means "deploy noise": on data with no edge, nothing is
deployed and the app stays on the transparent baseline.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from services.learning_engine.registry import ModelRegistry
from services.learning_engine.self_learning import detect_drift, run_training_cycle

logger = logging.getLogger("aiaura.autolearn")

# expiry -> analysis timeframe (must match the API's mapping)
EXPIRY_TIMEFRAME = {5: 5, 15: 5, 30: 10, 60: 15, 180: 30, 300: 60, 900: 60}


@dataclass
class AutoLearnConfig:
    targets: List[Tuple[str, int]] = field(default_factory=lambda: [("EURUSD_otc", 60)])
    interval_s: float = 1800.0        # base retrain cadence (30 min)
    warmup_delay_s: float = 120.0     # wait after startup before the first cycle
    pages: int = 8                    # candle history depth per target
    drift_interval_s: float = 60.0    # how often to check drift + loss trigger
    # Retrain after this many NEW losses. Lower = faster adaptation to regime
    # changes, but MORE frequent tests -> higher cumulative false-promotion risk
    # (the strict gate still guards each cycle). Owner set this to 5.
    loss_trigger: int = 5


class AutoLearner:
    def __init__(self, provider, store, registry_dir, on_promote: Callable[[str], None],
                 config: Optional[AutoLearnConfig] = None):
        self._provider = provider
        self._store = store
        self._registry = ModelRegistry(registry_dir)
        self._on_promote = on_promote
        self._cfg = config or AutoLearnConfig()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None
        self.last_train_at: float = 0.0
        self.cycles = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="auto-learner", daemon=True)
        self._thread.start()
        logger.info("auto-learner started (targets=%s, interval=%.0fs)",
                    self._cfg.targets, self._cfg.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        if self._stop.wait(self._cfg.warmup_delay_s):
            return
        last_drift_check = 0.0
        while not self._stop.is_set():
            try:
                self._cycle(reason="scheduled")
            except Exception as exc:
                logger.warning("auto-learn cycle failed: %s", exc)

            # Between full cycles, poll for (a) enough new losses to learn from,
            # and (b) performance drift — retrain early on either.
            elapsed = 0.0
            while elapsed < self._cfg.interval_s and not self._stop.is_set():
                if self._stop.wait(self._cfg.drift_interval_s):
                    return
                elapsed += self._cfg.drift_interval_s
                reason = None
                if self._losses_trigger():
                    reason = "losses"
                elif time.time() - last_drift_check >= self._cfg.drift_interval_s:
                    last_drift_check = time.time()
                    if self._drift_says_retrain():
                        reason = "drift"
                if reason:
                    try:
                        self._cycle(reason=reason)
                    except Exception as exc:
                        logger.warning("%s-triggered cycle failed: %s", reason, exc)
                    break  # restart the outer cadence after an early retrain

    def _losses_trigger(self) -> bool:
        """Fire after loss_trigger NEW losses since the last training cycle."""
        if self._store is None:
            return False
        try:
            total = self._store.total_losses()
            last = int(self._store.get_meta("trained_at_loss_count") or 0)
        except Exception:
            return False
        return (total - last) >= self._cfg.loss_trigger

    def _drift_says_retrain(self) -> bool:
        rec = self._registry.champion_record()
        if rec is None or self._store is None:
            return False
        baseline = (rec.metrics or {}).get("oos_win_rate")
        if not baseline:
            return False
        s = self._store.stats()
        if s.get("settled", 0) < 30:
            return False
        d = detect_drift(s["wins"], s["settled"], float(baseline))
        return d.get("status") == "drift"

    def _cycle(self, reason: str) -> None:
        if self._provider is None or not self._provider.is_connected():
            return
        for asset, expiry in self._cfg.targets:
            timeframe = EXPIRY_TIMEFRAME.get(expiry, 15)
            try:
                self._provider.subscribe(asset)
            except Exception:
                pass
            self._provider.wait_for_first_tick(asset, timeout_s=8)
            payout = 0.8
            info = self._provider.get_assets().get(asset)
            if info and info.payout:
                payout = float(info.payout) / 100.0
            candles = self._provider.get_historical_candles(asset, timeframe, pages=self._cfg.pages)
            res = run_training_cycle(candles, horizon_s=float(expiry),
                                     registry=self._registry, payout=payout)
            res["reason"] = reason
            res["asset"] = asset
            res["expiry_s"] = expiry
            self.last_result = res
            self.last_train_at = time.time()
            self.cycles += 1
            if self._store is not None:
                self._store.set_meta("last_auto_train", str(self.last_train_at))
                # reset the loss counter baseline so the next trigger is "new" losses
                try:
                    self._store.set_meta("trained_at_loss_count", str(self._store.total_losses()))
                except Exception:
                    pass
            logger.info("auto-learn (%s) %s/%ss: %s v=%s promoted=%s", reason, asset, expiry,
                        res.get("status"), res.get("version"), res.get("promoted"))
            if res.get("status") == "trained" and res.get("promoted"):
                logger.info("auto-learner promoted champion %s", res.get("version"))
                self._on_promote(res["version"])
