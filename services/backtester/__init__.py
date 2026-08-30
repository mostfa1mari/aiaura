"""Event-driven backtester (Phase 9).

Replays closed candles in time order, asks a signal function for BUY/SELL using
ONLY past candles, evaluates the outcome over a horizon with the same
reference-price rule as live labeling, and reports honest metrics (win rate with
a Wilson confidence interval, payout-adjusted expectancy, break-even, profit
factor, max losing streak, and significance vs break-even).

No look-ahead: features/signals at candle i use candles[:i+1]; the outcome uses
future closes but is the label, never an input to the signal.
"""

from services.backtester.engine import (
    BacktestConfig,
    BacktestResult,
    Trade,
    backtest,
    walk_forward_splits,
)

__all__ = ["BacktestConfig", "BacktestResult", "Trade", "backtest", "walk_forward_splits"]
