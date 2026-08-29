"""Baseline signal engine — transparent heuristic ensemble (Phase 8 slice).

Given recent CLOSED candles for one timeframe, computes a few classic
sub-signals (trend, momentum, price action, volatility-adjusted drift) and
combines them into a single directional score in [-1, +1]:

    score > 0  -> BUY
    score < 0  -> SELL
    score == 0 -> tie broken toward the last closed candle's direction

Per the product spec the UI shows only BUY/SELL (never WAIT), but uncertainty
is preserved internally as ``strength`` (|score|) and ``agreement`` (fraction
of sub-signals agreeing with the final side) and ``data_sufficiency``.

IMPORTANT: this is a baseline, not a validated edge. It makes NO probability
claim. It is deliberately simple and fully inspectable so results are honest;
ML replaces ``generate_signal``'s scoring later behind the same SignalResult.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from services.market_data.provider import CanonicalCandle

BASELINE_VERSION = "baseline-1.0.0"

# Minimum closed candles for each sub-signal to be meaningful.
_MIN_CANDLES = 30


@dataclass(frozen=True)
class SubSignal:
    name: str
    direction: int          # +1 BUY-ish, -1 SELL-ish, 0 neutral
    score: float            # contribution in [-1, +1]
    detail: str = ""


@dataclass(frozen=True)
class SignalResult:
    signal: str             # "BUY" | "SELL"
    score: float            # [-1, +1], sign = side
    strength: float         # |score| in [0, 1]
    agreement: float        # fraction of non-neutral sub-signals on the final side
    regime: str             # coarse market regime label
    data_sufficiency: float # 0..1 (1 = enough candles for all sub-signals)
    candles_used: int
    timeframe_s: int
    sub_signals: List[SubSignal] = field(default_factory=list)
    model_version: str = BASELINE_VERSION
    note: str = ""


# --- small, dependency-free indicators -------------------------------------

def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _roc(closes: Sequence[float], period: int = 10) -> Optional[float]:
    if len(closes) < period + 1 or closes[-period - 1] == 0:
        return None
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1]


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --- the baseline ensemble --------------------------------------------------

def generate_signal(candles: Sequence[CanonicalCandle], timeframe_s: int) -> SignalResult:
    """Compute a baseline BUY/SELL from CLOSED candles (oldest first).

    Forming candles must be excluded by the caller (they leak the future).
    """
    closed = [c for c in candles if c.complete]
    closes = [c.close for c in closed]
    n = len(closed)
    sufficiency = _clip(n / _MIN_CANDLES, 0.0, 1.0)

    subs: List[SubSignal] = []

    # 1) Trend: fast EMA vs slow EMA, normalized by recent volatility.
    ema_fast = _ema(closes, 9)
    ema_slow = _ema(closes, 21)
    vol = _stdev(closes[-21:]) if n >= 21 else _stdev(closes)
    if ema_fast is not None and ema_slow is not None and vol > 0:
        z = (ema_fast - ema_slow) / vol
        subs.append(SubSignal("trend_ema", _sign(z), _clip(z / 2.0),
                              f"emaFast-emaSlow={ema_fast - ema_slow:.6f}, vol={vol:.6f}"))

    # 2) EMA slope (persistence of direction).
    if n >= 21:
        es = _ema_series(closes, 9)
        if len(es) >= 4 and vol > 0:
            slope = (es[-1] - es[-4]) / vol
            subs.append(SubSignal("ema_slope", _sign(slope), _clip(slope / 2.0),
                                  f"slope3={slope:.4f}"))

    # 3) Momentum: RSI distance from 50.
    rsi = _rsi(closes, 14)
    if rsi is not None:
        subs.append(SubSignal("rsi", _sign(rsi - 50.0), _clip((rsi - 50.0) / 30.0),
                              f"rsi={rsi:.1f}"))

    # 4) Rate of change.
    roc = _roc(closes, 10)
    if roc is not None and vol > 0:
        subs.append(SubSignal("roc", _sign(roc), _clip((roc / vol) / 3.0),
                              f"roc10={roc:.6f}"))

    # 5) Price action: body direction of the last few closed candles.
    if n >= 3:
        recent = closed[-3:]
        body = sum((c.close - c.open) for c in recent)
        rng = sum((c.high - c.low) for c in recent) or 1e-9
        pa = _clip(body / rng)
        subs.append(SubSignal("price_action", _sign(pa), pa,
                              f"body/range(3)={pa:.3f}"))

    # Combine (equal-weight mean of available sub-scores).
    if subs:
        score = _clip(sum(s.score for s in subs) / len(subs))
    else:
        score = 0.0

    if score > 0:
        signal = "BUY"
    elif score < 0:
        signal = "SELL"
    else:
        # tie -> last closed candle direction; final fallback BUY (never WAIT)
        if closed and closed[-1].close < closed[-1].open:
            signal = "SELL"
        else:
            signal = "BUY"

    side = 1 if signal == "BUY" else -1
    non_neutral = [s for s in subs if s.direction != 0]
    agreement = (sum(1 for s in non_neutral if s.direction == side) / len(non_neutral)
                 if non_neutral else 0.0)

    regime = _regime(closes, vol)

    note = ""
    if sufficiency < 1.0:
        note = (f"baseline heuristic on {n} candles "
                f"(< {_MIN_CANDLES} recommended); low data sufficiency")

    return SignalResult(
        signal=signal,
        score=score,
        strength=abs(score),
        agreement=agreement,
        regime=regime,
        data_sufficiency=sufficiency,
        candles_used=n,
        timeframe_s=timeframe_s,
        sub_signals=subs,
        note=note,
    )


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _regime(closes: Sequence[float], vol: float) -> str:
    if len(closes) < 21:
        return "insufficient_data"
    ema_fast = _ema(closes, 9)
    ema_slow = _ema(closes, 21)
    mean = sum(closes[-21:]) / 21
    rel_vol = vol / mean if mean else 0.0
    trending = ema_fast is not None and ema_slow is not None and abs(ema_fast - ema_slow) > vol
    if rel_vol > 0.002:
        vlabel = "high_volatility"
    elif rel_vol < 0.0005:
        vlabel = "low_volatility"
    else:
        vlabel = "normal_volatility"
    if trending:
        direction = "up" if (ema_fast - ema_slow) > 0 else "down"
        return f"trend_{direction}"
    return f"range_{vlabel}"
