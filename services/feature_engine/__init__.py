"""Feature engine (Phase 7).

Turns a window of CLOSED candles (and optional multi-timeframe context) into a
named, versioned feature vector for strategies, the backtester, and ML. Every
feature uses only data at or before the current candle — no look-ahead.
"""

from services.feature_engine.features import (
    FEATURE_VERSION,
    FeatureVector,
    compute_features,
    feature_names,
)

__all__ = ["FEATURE_VERSION", "FeatureVector", "compute_features", "feature_names"]
