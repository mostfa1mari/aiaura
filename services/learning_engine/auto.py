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

import json
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
    interval_s: float = 900.0         # base retrain cadence (15 min) — near-continuous
    warmup_delay_s: float = 120.0     # wait after startup before the first cycle
    pages: int = 8                    # candle history depth per target
    drift_interval_s: float = 60.0    # how often to check drift + loss trigger
    # Retrain after this many NEW losses. Lower = faster adaptation to regime
    # changes, but MORE frequent tests -> higher cumulative false-promotion risk
    # (the strict gate still guards each cycle). Owner set this to 5.
    loss_trigger: int = 5
    # Dynamic targeting: each cycle, train every asset whose LIVE payout is at
    # least `payout_threshold` (so "the 92% pairs") at each of `target_expiries`.
    # Falls back to the static `targets` list if the catalog is unavailable.
    dynamic_high_payout: bool = True
    payout_threshold: float = 90.0
    target_expiries: List[int] = field(default_factory=lambda: [60])
    max_targets: int = 40             # safety cap on the high-payout universe
    per_cycle: int = 6                # train this many pairs per cycle, rotating
                                      # through the rest — keeps each cycle light
                                      # so it never starves the live /analyze path


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
        self.last_targets: List[Tuple[str, int]] = []
        self._rot = 0                 # rotation cursor over the target universe

    def _load_state(self) -> None:
        """Restore cycle count / last result across restarts so the dashboard
        shows continuity (the watchdog restarts the worker periodically)."""
        if self._store is None:
            return
        try:
            raw = self._store.get_meta("autolearn_state")
            if raw:
                d = json.loads(raw)
                self.cycles = int(d.get("cycles", 0))
                self.last_train_at = float(d.get("last_train_at", 0.0))
                self.last_result = d.get("last_result")
        except Exception:
            pass

    def _persist_state(self) -> None:
        if self._store is None:
            return
        try:
            self._store.set_meta("autolearn_state", json.dumps({
                "cycles": self.cycles,
                "last_train_at": self.last_train_at,
                "last_result": self.last_result,
            }))
        except Exception:
            pass

    def start(self) -> None:
        if self._thread is not None:
            return
        self._load_state()
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

    def _current_targets(self) -> List[Tuple[str, int]]:
        """The (asset, expiry) pairs to train THIS cycle. In dynamic mode, every
        asset whose live payout >= threshold (the "92% pairs") at each configured
        expiry; otherwise the static list. Falls back to static if the catalog
        can't be read."""
        if not self._cfg.dynamic_high_payout or self._provider is None:
            return list(self._cfg.targets)
        try:
            assets = self._provider.get_assets()
        except Exception:
            return list(self._cfg.targets)
        highs = sorted(
            sym for sym, info in (assets or {}).items()
            if getattr(info, "payout", None) and float(info.payout) >= self._cfg.payout_threshold)
        if not highs:
            return list(self._cfg.targets)
        targets = [(sym, exp) for sym in highs for exp in self._cfg.target_expiries]
        return targets[: self._cfg.max_targets]

    def _cycle(self, reason: str) -> None:
        if self._provider is None or not self._provider.is_connected():
            return
        full = self._current_targets()
        self.last_targets = full
        if not full:
            return
        # Train only a rotating BATCH per cycle so one pass stays light and never
        # starves the live /analyze path; successive cycles cover the rest.
        per = max(1, self._cfg.per_cycle)
        n = len(full)
        batch = [full[(self._rot + i) % n] for i in range(min(per, n))]
        self._rot = (self._rot + len(batch)) % n

        results = []
        promoted = None
        for asset, expiry in batch:
            if self._stop.is_set():
                break
            timeframe = EXPIRY_TIMEFRAME.get(expiry, 15)
            try:
                payout = 0.8
                info = self._provider.get_assets().get(asset)
                if info and info.payout:
                    payout = float(info.payout) / 100.0
                # Fetch history WITHOUT subscribing. subscribe() sends change_symbol
                # and would hijack the live stream away from the user's asset;
                # loadHistoryPeriod works for any asset once the stream tz is
                # detected (provided by the live/collector streams).
                candles = self._provider.get_historical_candles(asset, timeframe, pages=self._cfg.pages)
                res = run_training_cycle(candles, horizon_s=float(expiry),
                                         registry=self._registry, payout=payout)
            except Exception as exc:
                logger.warning("auto-learn target %s/%ss failed: %s", asset, expiry, exc)
                continue
            res["reason"] = reason
            res["asset"] = asset
            res["expiry_s"] = expiry
            results.append(res)
            logger.info("auto-learn (%s) %s/%ss: %s v=%s promoted=%s", reason, asset, expiry,
                        res.get("status"), res.get("version"), res.get("promoted"))
            if res.get("status") == "trained" and res.get("promoted"):
                promoted = res
                logger.info("auto-learner promoted champion %s (%s)", res.get("version"), asset)
                self._on_promote(res["version"])

        if not results:
            return
        # One cycle = one full pass over all high-payout targets. Summarize it so
        # the dashboard shows how many pairs were trained and whether any promoted.
        self.cycles += 1
        self.last_train_at = time.time()
        self.last_result = promoted or _summarize(results, reason, len(batch))
        self._persist_state()  # survive watchdog/deploy restarts
        if self._store is not None:
            self._store.set_meta("last_auto_train", str(self.last_train_at))
            try:  # reset the loss-trigger baseline so the next trigger is "new" losses
                self._store.set_meta("trained_at_loss_count", str(self._store.total_losses()))
            except Exception:
                pass


def _summarize(results: List[dict], reason: str, n_targets: int) -> dict:
    """Roll a full training pass into one honest status: how many pairs trained,
    how many promoted, and the strongest challenger this pass (so the dashboard
    shows the model IS training even when nothing clears the strict gate)."""
    trained = [r for r in results if r.get("status") == "trained"]
    def _exp(r):
        return (r.get("metrics") or {}).get("oos_expectancy", -9.0) or -9.0
    best = max(trained, key=_exp, default=None)
    summary = dict(best) if best else dict(results[-1])
    summary["reason"] = reason
    summary["targets_trained"] = len(trained)
    summary["targets_total"] = n_targets
    summary["promoted_count"] = sum(1 for r in results if r.get("promoted"))
    return summary
