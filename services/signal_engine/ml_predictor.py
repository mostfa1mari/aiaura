"""ML predictor — wraps a trained champion model behind SignalResult (Phase 11).

Produces the same SignalResult shape as the baseline, so the app can use ML when
a champion exists and fall back to the baseline otherwise. Never fabricates a
probability claim: ``strength`` is the model's distance from 0.5, and the record
carries the model's honest out-of-sample metrics for the dashboard.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from services.feature_engine import FEATURE_VERSION, compute_features, feature_names
from services.market_data.provider import CanonicalCandle
from services.signal_engine.baseline import SignalResult, generate_signal


class MLPredictor:
    def __init__(self, model, model_version: str, names: Optional[Sequence[str]] = None):
        self._model = model
        self.model_version = model_version
        self._names = list(names) if names else feature_names()

    def predict(self, candles: Sequence[CanonicalCandle], timeframe_s: int = 0) -> SignalResult:
        closed = [c for c in candles if c.complete]
        fv = compute_features(closed, timeframe_s=timeframe_s)
        row = fv.as_row(self._names)
        try:
            prob_up = float(self._model.predict_proba([row])[0][1])
        except Exception:
            # model without predict_proba -> fall back to decision
            pred = int(self._model.predict([row])[0])
            prob_up = 1.0 if pred == 1 else 0.0

        signal = "BUY" if prob_up >= 0.5 else "SELL"
        strength = abs(prob_up - 0.5) * 2.0
        # keep the baseline's regime/agreement context for display
        base = generate_signal(closed, timeframe_s or (closed[-1].timeframe_s if closed else 0))
        return SignalResult(
            signal=signal,
            score=(prob_up - 0.5) * 2.0,
            strength=strength,
            agreement=1.0 if signal == base.signal else 0.0,  # ML vs baseline concordance
            regime=base.regime,
            data_sufficiency=base.data_sufficiency,
            candles_used=len(closed),
            timeframe_s=timeframe_s or base.timeframe_s,
            sub_signals=base.sub_signals,
            model_version=self.model_version,
            note=f"ML prob_up={prob_up:.3f}; feature {FEATURE_VERSION}",
        )
