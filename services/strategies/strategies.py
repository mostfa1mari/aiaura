"""Independent strategy modules + an equal-weight ensemble (Phase 8).

Each strategy reads the no-look-ahead feature dict (services/feature_engine) and
returns a StrategyResult(direction in {-1,0,+1}, score in [-1,+1]). A strategy
returns 0/NEUTRAL when its precondition (the features it needs) is absent, so it
never guesses. ``ensemble`` averages the non-neutral scores into one BUY/SELL
with an agreement measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

from services.feature_engine import compute_features
from services.market_data.provider import CanonicalCandle

STRATEGY_VERSION = "strategies-1.0.0"


@dataclass(frozen=True)
class StrategyResult:
    name: str
    direction: int          # +1 BUY, -1 SELL, 0 neutral
    score: float            # [-1, +1]
    version: str = STRATEGY_VERSION


@dataclass
class EnsembleResult:
    signal: str             # BUY | SELL
    score: float
    strength: float
    agreement: float
    contributors: int
    per_strategy: List[StrategyResult] = field(default_factory=list)
    version: str = STRATEGY_VERSION


def _clip(x: float, lo=-1.0, hi=1.0) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


# --- strategies (each: features dict -> StrategyResult) ---------------------

def trend_following(f: Dict[str, float]) -> StrategyResult:
    if "trend_ema9_minus_ema21" not in f:
        return StrategyResult("trend_following", 0, 0.0)
    s = _clip(f["trend_ema9_minus_ema21"] + f.get("trend_ema9_slope3", 0.0))
    return StrategyResult("trend_following", _sign(s), s)


def momentum(f: Dict[str, float]) -> StrategyResult:
    if "mom_rsi14_centered" not in f:
        return StrategyResult("momentum", 0, 0.0)
    s = _clip(f["mom_rsi14_centered"] + f.get("mom_roc10", 0.0) * 2 + f.get("mom_macd_hist", 0.0))
    return StrategyResult("momentum", _sign(s), s)


def mean_reversion(f: Dict[str, float]) -> StrategyResult:
    rsi = f.get("mom_rsi14")
    if rsi is None:
        return StrategyResult("mean_reversion", 0, 0.0)
    # fade extremes: oversold -> BUY, overbought -> SELL
    if rsi < 30:
        return StrategyResult("mean_reversion", 1, _clip((30 - rsi) / 30))
    if rsi > 70:
        return StrategyResult("mean_reversion", -1, -_clip((rsi - 70) / 30))
    return StrategyResult("mean_reversion", 0, 0.0)


def breakout(f: Dict[str, float]) -> StrategyResult:
    up = f.get("pa_breakout_up", 0.0)
    down = f.get("pa_breakout_down", 0.0)
    if up:
        return StrategyResult("breakout", 1, 0.8)
    if down:
        return StrategyResult("breakout", -1, -0.8)
    return StrategyResult("breakout", 0, 0.0)


def reversal(f: Dict[str, float]) -> StrategyResult:
    # long lower wick after weakness -> BUY; long upper wick after strength -> SELL
    lw = f.get("pa_lower_wick_over_range", 0.0)
    uw = f.get("pa_upper_wick_over_range", 0.0)
    rsi = f.get("mom_rsi14", 50.0)
    if lw > 0.5 and rsi < 45:
        return StrategyResult("reversal", 1, _clip(lw))
    if uw > 0.5 and rsi > 55:
        return StrategyResult("reversal", -1, -_clip(uw))
    return StrategyResult("reversal", 0, 0.0)


def support_resistance(f: Dict[str, float]) -> StrategyResult:
    sup = f.get("struct_support_dist")
    res = f.get("struct_resistance_dist")
    # near support (small positive dist above support) -> BUY; near resistance -> SELL
    if sup is not None and res is not None:
        if sup < res and sup < 0.5:
            return StrategyResult("support_resistance", 1, _clip(0.5 - sup))
        if res < sup and res < 0.5:
            return StrategyResult("support_resistance", -1, -_clip(0.5 - res))
    return StrategyResult("support_resistance", 0, 0.0)


def price_action(f: Dict[str, float]) -> StrategyResult:
    if "pa_body_over_range" not in f:
        return StrategyResult("price_action", 0, 0.0)
    s = _clip(f["pa_body_over_range"] + 0.1 * f.get("pa_consecutive", 0.0))
    return StrategyResult("price_action", _sign(s), s)


def volatility_breakout(f: Dict[str, float]) -> StrategyResult:
    exp = f.get("vol_expansion")
    if exp is None or exp <= 1.2:  # only act when volatility is expanding
        return StrategyResult("volatility_breakout", 0, 0.0)
    # trade in the direction of the current body
    body = f.get("pa_body_over_range", 0.0)
    return StrategyResult("volatility_breakout", _sign(body), _clip(body))


def adx_trend(f: Dict[str, float]) -> StrategyResult:
    adx = f.get("adx14")
    if adx is None or adx < 25:  # only in a real trend
        return StrategyResult("adx_trend", 0, 0.0)
    bull = f.get("adx_bull", 0.0)
    d = 1 if bull else -1
    return StrategyResult("adx_trend", d, _clip(d * min(adx / 50, 1.0)))


STRATEGIES: List[Tuple[str, Callable[[Dict[str, float]], StrategyResult]]] = [
    ("trend_following", trend_following),
    ("momentum", momentum),
    ("mean_reversion", mean_reversion),
    ("breakout", breakout),
    ("reversal", reversal),
    ("support_resistance", support_resistance),
    ("price_action", price_action),
    ("volatility_breakout", volatility_breakout),
    ("adx_trend", adx_trend),
]


def ensemble(features: Dict[str, float]) -> EnsembleResult:
    results = [fn(features) for _, fn in STRATEGIES]
    active = [r for r in results if r.direction != 0]
    if active:
        score = _clip(sum(r.score for r in active) / len(active))
    else:
        score = 0.0
    signal = "BUY" if score > 0 else ("SELL" if score < 0 else "BUY")
    side = 1 if signal == "BUY" else -1
    agreement = (sum(1 for r in active if r.direction == side) / len(active)) if active else 0.0
    return EnsembleResult(
        signal=signal, score=score, strength=abs(score), agreement=agreement,
        contributors=len(active), per_strategy=results,
    )


def strategy_signal_fn(strategy: Callable[[Dict[str, float]], StrategyResult]):
    """Adapt a single strategy to the backtester's signal_fn(features, candles)."""
    def _fn(features: Dict[str, float], candles: Sequence[CanonicalCandle]):
        r = strategy(features)
        side = "BUY" if r.direction > 0 else ("SELL" if r.direction < 0 else "BUY")
        return side, abs(r.score)
    return _fn


def ensemble_signal_fn(features: Dict[str, float], candles: Sequence[CanonicalCandle]):
    e = ensemble(features)
    return e.signal, e.strength
