"""Technical indicators — pure functions over price sequences.

All operate on plain float lists ordered oldest→newest and return either a
single value "as of the last element" or a same-length series. None is returned
when there is insufficient history. No function looks past the end of its input,
so composing them on candles[:i+1] is inherently no-look-ahead.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return ema_series(values, period)[-1]


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
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


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Optional[Tuple[float, float, float]]:
    """Returns (macd_line, signal_line, histogram) as of the last close."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line_series = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_series = ema_series(macd_line_series, signal)
    line = macd_line_series[-1]
    sig = signal_series[-1]
    return line, sig, line - sig


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int = 14) -> Optional[float]:
    """%K stochastic oscillator (0..100)."""
    if len(closes) < period:
        return None
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    if hh == ll:
        return 50.0
    return (closes[-1] - ll) / (hh - ll) * 100.0


def roc(closes: Sequence[float], period: int = 10) -> Optional[float]:
    if len(closes) < period + 1 or closes[-period - 1] == 0:
        return None
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1]


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    tr: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return tr


def atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    tr = true_ranges(highs, lows, closes)
    return sum(tr[-period:]) / period


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def realized_volatility(closes: Sequence[float], period: int = 20) -> Optional[float]:
    """Stdev of simple returns over the last `period` closes."""
    if len(closes) < period + 1:
        return None
    window = closes[-period - 1:]
    rets = [(window[i] - window[i - 1]) / window[i - 1]
            for i in range(1, len(window)) if window[i - 1] != 0]
    return stdev(rets) if rets else None


def bollinger_width(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> Optional[float]:
    """(upper-lower)/middle — normalized band width."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    sd = stdev(window)
    if mid == 0:
        return None
    return (2 * mult * sd) / mid


def adx(highs, lows, closes, period: int = 14) -> Optional[Tuple[float, float, float]]:
    """Returns (adx, plus_di, minus_di). Wilder-style smoothing (SMA variant)."""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    plus_dm, minus_dm = [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    tr = true_ranges(highs, lows, closes)[1:]  # align with dm (len n-1)

    def smooth(x):
        return sum(x[-period:]) / period

    atr_v = smooth(tr)
    if atr_v == 0:
        return None
    plus_di = 100.0 * smooth(plus_dm) / atr_v
    minus_di = 100.0 * smooth(minus_dm) / atr_v
    denom = plus_di + minus_di
    dx = 100.0 * abs(plus_di - minus_di) / denom if denom else 0.0
    # crude ADX: DX smoothed over recent window
    dxs = []
    for j in range(period, len(tr) + 1):
        w_tr = sum(tr[j - period:j]) / period
        if w_tr == 0:
            continue
        pdi = 100.0 * sum(plus_dm[j - period:j]) / period / w_tr
        mdi = 100.0 * sum(minus_dm[j - period:j]) / period / w_tr
        d = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / d if d else 0.0)
    adx_v = sum(dxs[-period:]) / min(len(dxs), period) if dxs else dx
    return adx_v, plus_di, minus_di


def swing_points(highs: Sequence[float], lows: Sequence[float], left: int = 2, right: int = 2
                 ) -> Tuple[List[int], List[int]]:
    """Indices of swing highs and swing lows (fractal-style).

    A swing high at i has highs[i] strictly greater than `left` bars before and
    `right` bars after. Only indices with a full right-window are considered, so
    the result is confirmed and no-look-ahead relative to the last such index.
    """
    sh, sl = [], []
    n = len(highs)
    for i in range(left, n - right):
        window_h = highs[i - left:i + right + 1]
        window_l = lows[i - left:i + right + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            sh.append(i)
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            sl.append(i)
    return sh, sl
