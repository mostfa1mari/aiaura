"""Signal engine (baseline).

Produces a BUY/SELL directional signal from recent candles. This is a
transparent BASELINE ensemble of classic heuristics — NOT a validated model
and NOT financial advice. It exists so the app has an honest, data-driven
signal now; ML models (Phases 10-11) replace the scoring later behind the same
interface. It never claims a win rate and never places trades.
"""

from services.signal_engine.baseline import (
    BASELINE_VERSION,
    SignalResult,
    SubSignal,
    generate_signal,
)

__all__ = ["BASELINE_VERSION", "SignalResult", "SubSignal", "generate_signal"]
