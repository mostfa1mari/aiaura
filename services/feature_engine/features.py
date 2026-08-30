"""Feature vector construction (Phase 7).

``compute_features(candles)`` returns a flat, named, versioned feature vector
computed AS OF the last candle in the list. Callers pass only closed candles up
to the decision time T, which makes every feature no-look-ahead by construction.

Groups: trend, momentum, volatility, trend-strength, price-action, structure.
Missing features (insufficient history) are omitted from the dict; use
``feature_names()`` + a fixed order (with 0.0 fill) when building an ML matrix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from services.feature_engine import indicators as ind
from services.market_data.provider import CanonicalCandle

FEATURE_VERSION = "features-1.0.0"


@dataclass
class FeatureVector:
    asset: str
    timeframe_s: int
    at_timestamp: float          # timestamp of the last (current) candle
    values: Dict[str, float] = field(default_factory=dict)
    version: str = FEATURE_VERSION

    def get(self, name: str, default: float = 0.0) -> float:
        return self.values.get(name, default)

    def as_row(self, names: Sequence[str]) -> List[float]:
        """Dense row in the given name order (0.0 for missing)."""
        return [float(self.values.get(n, 0.0)) for n in names]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def compute_features(candles: Sequence[CanonicalCandle], asset: str = "", timeframe_s: int = 0
                     ) -> FeatureVector:
    closed = [c for c in candles if c.complete]
    at_ts = closed[-1].timestamp if closed else 0.0
    asset = asset or (closed[-1].asset if closed else "")
    timeframe_s = timeframe_s or (closed[-1].timeframe_s if closed else 0)
    fv = FeatureVector(asset=asset, timeframe_s=timeframe_s, at_timestamp=at_ts)
    v = fv.values
    if len(closed) < 2:
        return fv

    opens = [c.open for c in closed]
    highs = [c.high for c in closed]
    lows = [c.low for c in closed]
    closes = [c.close for c in closed]

    def put(name, val, scale=None):
        if val is None or not _finite(val):
            return
        if scale is not None:
            if scale == 0 or not _finite(scale):
                return
            val = val / scale
        v[name] = float(val)

    last_close = closes[-1]
    vol = ind.stdev(closes[-20:]) if len(closes) >= 2 else 0.0
    price_scale = vol if vol > 0 else (abs(last_close) or 1.0)

    # --- Trend -------------------------------------------------------
    ema9 = ind.ema(closes, 9)
    ema21 = ind.ema(closes, 21)
    sma50 = ind.sma(closes, 50)
    put("trend_ema9_rel", (ema9 - last_close) if ema9 is not None else None, price_scale)
    put("trend_ema21_rel", (ema21 - last_close) if ema21 is not None else None, price_scale)
    if ema9 is not None and ema21 is not None:
        put("trend_ema9_minus_ema21", ema9 - ema21, price_scale)
        v["trend_ema9_above_ema21"] = 1.0 if ema9 > ema21 else 0.0
    if sma50 is not None:
        put("trend_close_minus_sma50", last_close - sma50, price_scale)
    es = ind.ema_series(closes, 9)
    if len(es) >= 4:
        put("trend_ema9_slope3", es[-1] - es[-4], price_scale)
    # trend persistence: fraction of last 10 closes above ema9
    if ema9 is not None and len(closes) >= 10:
        v["trend_persistence10"] = sum(1 for c in closes[-10:] if c > ema9) / 10.0

    # --- Momentum ----------------------------------------------------
    put("mom_rsi14", ind.rsi(closes, 14))
    r = ind.rsi(closes, 14)
    if r is not None:
        put("mom_rsi14_centered", (r - 50.0) / 50.0)
    m = ind.macd(closes)
    if m is not None:
        line, sig, hist = m
        put("mom_macd_line", line, price_scale)
        put("mom_macd_hist", hist, price_scale)
        v["mom_macd_bull"] = 1.0 if line > sig else 0.0
    put("mom_stoch14", ind.stochastic(highs, lows, closes, 14))
    put("mom_roc10", ind.roc(closes, 10))

    # --- Volatility --------------------------------------------------
    put("vol_atr14", ind.atr(highs, lows, closes, 14), price_scale)
    put("vol_realized20", ind.realized_volatility(closes, 20))
    put("vol_bollinger_width20", ind.bollinger_width(closes, 20))
    # volatility expansion: recent realized vol vs longer
    rv_s = ind.realized_volatility(closes, 10)
    rv_l = ind.realized_volatility(closes, 30)
    if rv_s is not None and rv_l is not None and rv_l > 0:
        v["vol_expansion"] = rv_s / rv_l

    # --- Trend strength (ADX / DI) -----------------------------------
    a = ind.adx(highs, lows, closes, 14)
    if a is not None:
        adx_v, pdi, mdi = a
        put("adx14", adx_v)
        put("adx_plus_di", pdi)
        put("adx_minus_di", mdi)
        v["adx_bull"] = 1.0 if pdi > mdi else 0.0

    # --- Price action (last candle + recent) -------------------------
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    rng = (h - l) or 1e-12
    body = c - o
    put("pa_body_rel", body, price_scale)
    v["pa_body_over_range"] = _finite_ratio(body, rng)
    v["pa_upper_wick_over_range"] = _finite_ratio(h - max(o, c), rng)
    v["pa_lower_wick_over_range"] = _finite_ratio(min(o, c) - l, rng)
    put("pa_range_rel", h - l, price_scale)
    v["pa_bull"] = 1.0 if c > o else 0.0
    # consecutive same-direction closed candles
    v["pa_consecutive"] = float(_consecutive(closes))
    # breakout: close beyond the prior N-bar high/low
    if len(closes) >= 21:
        prior_high = max(highs[-21:-1])
        prior_low = min(lows[-21:-1])
        v["pa_breakout_up"] = 1.0 if c > prior_high else 0.0
        v["pa_breakout_down"] = 1.0 if c < prior_low else 0.0

    # --- Market structure -------------------------------------------
    sh, sl = ind.swing_points(highs, lows, 2, 2)
    if sh:
        last_sh = highs[sh[-1]]
        put("struct_dist_last_swing_high", c - last_sh, price_scale)
    if sl:
        last_sl = lows[sl[-1]]
        put("struct_dist_last_swing_low", c - last_sl, price_scale)
    # nearest support/resistance from recent swing clusters
    res = min((highs[i] for i in sh if highs[i] >= c), default=None)
    sup = max((lows[i] for i in sl if lows[i] <= c), default=None)
    if res is not None:
        put("struct_resistance_dist", res - c, price_scale)
    if sup is not None:
        put("struct_support_dist", c - sup, price_scale)
    # range detection: (max-min)/price over last 20
    if len(closes) >= 20:
        rr = (max(highs[-20:]) - min(lows[-20:]))
        v["struct_range20_rel"] = _finite_ratio(rr, abs(c) or 1.0)

    return fv


def _finite_ratio(num: float, den: float) -> float:
    if den == 0 or not math.isfinite(den):
        return 0.0
    r = num / den
    return r if math.isfinite(r) else 0.0


def _consecutive(closes: Sequence[float]) -> int:
    """Signed count of consecutive up(+)/down(-) closes ending at the last."""
    if len(closes) < 2:
        return 0
    direction = 1 if closes[-1] > closes[-2] else (-1 if closes[-1] < closes[-2] else 0)
    if direction == 0:
        return 0
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        step = 1 if closes[i] > closes[i - 1] else (-1 if closes[i] < closes[i - 1] else 0)
        if step == direction:
            count += 1
        else:
            break
    return direction * count


# A stable, ordered list of every feature this version can emit — used to build
# dense ML matrices with consistent columns (0.0 fill for missing).
_ALL_FEATURE_NAMES: List[str] = [
    "trend_ema9_rel", "trend_ema21_rel", "trend_ema9_minus_ema21",
    "trend_ema9_above_ema21", "trend_close_minus_sma50", "trend_ema9_slope3",
    "trend_persistence10",
    "mom_rsi14", "mom_rsi14_centered", "mom_macd_line", "mom_macd_hist",
    "mom_macd_bull", "mom_stoch14", "mom_roc10",
    "vol_atr14", "vol_realized20", "vol_bollinger_width20", "vol_expansion",
    "adx14", "adx_plus_di", "adx_minus_di", "adx_bull",
    "pa_body_rel", "pa_body_over_range", "pa_upper_wick_over_range",
    "pa_lower_wick_over_range", "pa_range_rel", "pa_bull", "pa_consecutive",
    "pa_breakout_up", "pa_breakout_down",
    "struct_dist_last_swing_high", "struct_dist_last_swing_low",
    "struct_resistance_dist", "struct_support_dist", "struct_range20_rel",
]


def feature_names() -> List[str]:
    """Stable ordered feature-name list for dense ML matrices."""
    return list(_ALL_FEATURE_NAMES)
