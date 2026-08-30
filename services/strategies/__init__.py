"""Baseline strategy modules (Phase 8).

Each strategy is an independent, inspectable function over a feature vector that
returns a direction and a score. They can be backtested individually
(services/backtester) and combined by ``ensemble`` into a single BUY/SELL. None
of them claims an edge — they are hypotheses to test, not signals to trust.
"""

from services.strategies.strategies import (
    STRATEGIES,
    STRATEGY_VERSION,
    EnsembleResult,
    StrategyResult,
    ensemble,
    strategy_signal_fn,
)

__all__ = [
    "STRATEGIES",
    "STRATEGY_VERSION",
    "EnsembleResult",
    "StrategyResult",
    "ensemble",
    "strategy_signal_fn",
]
