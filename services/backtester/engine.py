"""Backtest engine + metrics."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from services.feature_engine import compute_features
from services.labeling.target import Direction
from services.market_data.provider import CanonicalCandle

# signal_fn(features_dict, candles_so_far) -> ("BUY"|"SELL", strength[0..1])
SignalFn = Callable[[dict, Sequence[CanonicalCandle]], Tuple[str, float]]


@dataclass
class BacktestConfig:
    horizon_s: float                 # expiry length
    payout: float = 0.8              # net profit fraction on a win (0.8 = 80%)
    warmup: int = 50                 # candles before the first decision
    min_strength: float = 0.0        # skip signals weaker than this
    flat_is_loss: bool = True        # a tie (no move) counts as a loss
    price_precision: Optional[int] = None


@dataclass
class Trade:
    entry_time: float
    signal: str                      # BUY | SELL
    strength: float
    outcome: Direction               # UP | DOWN | FLAT
    won: bool
    hour: int                        # UTC hour of entry (time-of-day analysis)


@dataclass
class BacktestResult:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    skipped_no_data: int = 0
    skipped_weak: int = 0
    win_rate: Optional[float] = None
    wilson_low: Optional[float] = None
    wilson_high: Optional[float] = None
    payout: float = 0.8
    breakeven_win_rate: Optional[float] = None
    expectancy: Optional[float] = None      # per-trade EV in stake units
    profit_factor: Optional[float] = None
    max_losing_streak: int = 0
    z_vs_breakeven: Optional[float] = None
    p_value_one_sided: Optional[float] = None
    overlap: float = 1.0                     # horizon / candle-step
    effective_n: Optional[float] = None      # de-overlapped independent sample
    by_hour: Dict[int, dict] = field(default_factory=dict)
    by_signal: Dict[str, dict] = field(default_factory=dict)
    note: str = ""

    @property
    def has_edge_evidence(self) -> bool:
        """True only if the win rate is significantly above break-even
        (p<0.05, computed on the EFFECTIVE de-overlapped sample) AND expectancy
        is positive AND the effective sample is large enough. Deliberately
        strict — and still only *evidence*, to be confirmed by forward live
        performance, never a guarantee."""
        return bool(
            self.p_value_one_sided is not None
            and self.p_value_one_sided < 0.05
            and (self.expectancy or 0) > 0
            and (self.effective_n or 0) >= 100
        )

    def summary(self) -> dict:
        return {
            "trades": self.trades,
            "effective_n": self.effective_n,
            "overlap": self.overlap,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": _round(self.win_rate),
            "wilson_ci": [_round(self.wilson_low), _round(self.wilson_high)],
            "breakeven_win_rate": _round(self.breakeven_win_rate),
            "expectancy": _round(self.expectancy, 4),
            "profit_factor": _round(self.profit_factor),
            "max_losing_streak": self.max_losing_streak,
            "p_value_one_sided": _round(self.p_value_one_sided, 4),
            "has_edge_evidence": self.has_edge_evidence,
            "note": self.note,
        }


def _round(x, n=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _wilson(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


class _CloseReference:
    """Quote-in-effect lookup over candle CLOSES (no look-ahead: last close at
    or before t; requires the series to cover t)."""

    def __init__(self, candles: Sequence[CanonicalCandle]):
        pts = sorted((c.timestamp, c.close) for c in candles)
        self._ts = [p[0] for p in pts]
        self._px = [p[1] for p in pts]

    def price_at(self, t: float) -> Optional[float]:
        if not self._ts or t < self._ts[0] or t > self._ts[-1]:
            return None
        i = bisect.bisect_right(self._ts, t) - 1
        return self._px[i]


def backtest(candles: Sequence[CanonicalCandle], signal_fn: SignalFn,
             config: BacktestConfig) -> BacktestResult:
    closed = [c for c in candles if c.complete]
    closed.sort(key=lambda c: c.timestamp)
    result = BacktestResult(payout=config.payout)
    if len(closed) <= config.warmup:
        result.note = "insufficient candles for warmup"
        return result

    ref = _CloseReference(closed)
    trades: List[Trade] = []
    prec = config.price_precision
    # candle step -> overlap factor: trades spaced 1 candle apart but each spans
    # horizon_s, so consecutive outcomes overlap when horizon > step.
    step = (closed[1].timestamp - closed[0].timestamp) if len(closed) > 1 else config.horizon_s
    overlap = max(1.0, config.horizon_s / step) if step > 0 else 1.0

    for i in range(config.warmup, len(closed)):
        entry = closed[i]
        # Features/signal use ONLY candles up to and including i (no look-ahead).
        fv = compute_features(closed[: i + 1])
        signal, strength = signal_fn(fv.values, closed[: i + 1])
        if signal not in ("BUY", "SELL"):
            continue
        if strength < config.min_strength:
            result.skipped_weak += 1
            continue

        entry_price = ref.price_at(entry.timestamp)
        expiry_price = ref.price_at(entry.timestamp + config.horizon_s)
        if entry_price is None or expiry_price is None:
            result.skipped_no_data += 1
            continue

        e = round(entry_price, prec) if prec is not None else entry_price
        x = round(expiry_price, prec) if prec is not None else expiry_price
        outcome = Direction.UP if x > e else (Direction.DOWN if x < e else Direction.FLAT)

        if outcome is Direction.FLAT:
            won = False if config.flat_is_loss else None
            if won is None:
                continue  # tie excluded from win/loss when not counted
        else:
            won = (signal == "BUY" and outcome is Direction.UP) or \
                  (signal == "SELL" and outcome is Direction.DOWN)

        hour = datetime.fromtimestamp(entry.timestamp, tz=timezone.utc).hour
        trades.append(Trade(entry.timestamp, signal, strength, outcome, bool(won), hour))

    _finalize(result, trades, config, overlap)
    return result


def _finalize(result: BacktestResult, trades: List[Trade], config: BacktestConfig,
              overlap: float) -> None:
    result.trades = len(trades)
    result.wins = sum(1 for t in trades if t.won)
    result.losses = result.trades - result.wins
    result.overlap = round(overlap, 2)
    n = result.trades
    if n == 0:
        result.note = result.note or "no trades taken"
        return

    wr = result.wins / n
    result.win_rate = wr
    p = config.payout
    result.breakeven_win_rate = 1.0 / (1.0 + p) if p > 0 else None
    result.expectancy = wr * p - (1 - wr)          # stake units per trade (point est.)
    result.profit_factor = (result.wins * p) / result.losses if result.losses else float("inf")

    # Effective (de-overlapped) sample: consecutive trades share their outcome
    # window when horizon > candle step, so they are NOT independent. Confidence
    # and significance are computed on n_eff = n / overlap, never on raw n.
    n_eff = max(1.0, n / max(1.0, overlap))
    result.effective_n = round(n_eff, 1)
    eff_wins = wr * n_eff
    result.wilson_low, result.wilson_high = _wilson(eff_wins, n_eff)

    # max losing streak
    streak = worst = 0
    for t in trades:
        if not t.won:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    result.max_losing_streak = worst

    # one-sided significance vs break-even (H1: wr > breakeven), on effective N
    be = result.breakeven_win_rate
    if be is not None and 0 < be < 1:
        se = math.sqrt(be * (1 - be) / n_eff)
        if se > 0:
            z = (wr - be) / se
            result.z_vs_breakeven = z
            result.p_value_one_sided = 1.0 - _norm_cdf(z)

    # breakdowns
    result.by_hour = _group(trades, key=lambda t: t.hour)
    result.by_signal = _group(trades, key=lambda t: t.signal)

    if n_eff < 100:
        result.note = (f"small effective sample (n_eff={n_eff:.0f} from {n} "
                       f"overlapping trades); metrics not yet reliable")


def _group(trades: List[Trade], key) -> Dict:
    groups: Dict = {}
    for t in trades:
        k = key(t)
        g = groups.setdefault(k, {"trades": 0, "wins": 0})
        g["trades"] += 1
        g["wins"] += 1 if t.won else 0
    for g in groups.values():
        g["win_rate"] = round(g["wins"] / g["trades"], 4) if g["trades"] else None
    return dict(sorted(groups.items()))


def walk_forward_splits(n: int, folds: int = 5, min_train: int = 100
                        ) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Chronological expanding-window walk-forward splits over n samples.

    Returns [((train_start, train_end), (test_start, test_end)), ...] with test
    windows strictly AFTER their train window (no shuffling, no leakage).
    """
    if n < min_train + folds:
        return []
    test_size = (n - min_train) // folds
    if test_size <= 0:
        return []
    splits = []
    train_end = min_train
    for _ in range(folds):
        test_start = train_end
        test_end = min(test_start + test_size, n)
        if test_start >= test_end:
            break
        splits.append(((0, train_end), (test_start, test_end)))
        train_end = test_end
    return splits
