"""
Indicator math.

Pure numpy/Python helpers for the 8 criteria. No I/O, no async, no dependencies
on cero internals — everything in here takes lists or arrays of floats and
returns lists, arrays, or simple dataclasses.

Every function is small enough to read in one sitting; the formulas are
commented next to the code so the math is self-documenting. Where a name has
a standard definition (EMA, ATR Wilder), the convention used is noted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

import numpy as np

Trend = Literal["up", "down", "flat"]


# ──────────────────────────────────────────────────────────────────────
# Moving averages & volatility
# ──────────────────────────────────────────────────────────────────────


def ema(values: Sequence[float], period: int) -> np.ndarray:
    """Exponential moving average. Seed with the SMA of the first `period`
    values; this is the standard "true EMA" used by TradingView and most TA
    libraries (vs. seeding with values[0] which biases the first ~period bars).

    Returns an array the same length as `values`; the first `period - 1`
    entries are NaN."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    if period <= 0 or n == 0:
        raise ValueError("ema: need period > 0 and non-empty values")
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = v[:period].mean()
    for i in range(period, n):
        out[i] = alpha * v[i] + (1.0 - alpha) * out[i - 1]
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> np.ndarray:
    """Average True Range using Wilder's smoothing.

    TR_i = max(high_i - low_i, |high_i - close_{i-1}|, |low_i - close_{i-1}|)
    ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period   (Wilder)

    Returns an array the same length as the inputs; the first `period`
    entries (index 0..period-1) are NaN."""
    h = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(h)
    if not (n == len(low) == len(c)):
        raise ValueError("atr: highs/lows/closes must have equal length")
    if n < period + 1:
        return np.full(n, np.nan)

    tr = np.empty(n)
    tr[0] = h[0] - low[0]
    tr[1:] = np.maximum.reduce([
        h[1:] - low[1:],
        np.abs(h[1:] - c[:-1]),
        np.abs(low[1:] - c[:-1]),
    ])

    out = np.full(n, np.nan)
    # Seed with simple mean of first `period` TR values (Wilder's convention).
    out[period] = tr[1 : period + 1].mean()
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


# ──────────────────────────────────────────────────────────────────────
# Trend classification
# ──────────────────────────────────────────────────────────────────────


def classify_trend(
    closes: Sequence[float],
    period: int = 50,
    slope_lookback: int = 5,
    min_slope_frac: float = 0.001,
) -> Trend:
    """Classify the current trend using EMA(period) and its recent slope.

    Rules (from docs/CRITERIA.md):
      - price > EMA AND EMA slope ↑ enough  →  "up"
      - price < EMA AND EMA slope ↓ enough  →  "down"
      - otherwise                            →  "flat"

    "Enough" is defined by `min_slope_frac`: the absolute slope over
    `slope_lookback` bars must exceed this fraction of the current EMA value
    (default 0.1%). Without this threshold a pure random walk can land in
    "up" or "down" by chance — and "flat" is the safe default (don't trade).
    """
    c = np.asarray(closes, dtype=float)
    if len(c) < period + slope_lookback:
        return "flat"
    e = ema(c, period)
    last_price = float(c[-1])
    last_ema = float(e[-1])
    if np.isnan(last_ema):
        return "flat"
    prev_ema = float(e[-1 - slope_lookback])
    if np.isnan(prev_ema):
        return "flat"
    slope = last_ema - prev_ema
    min_abs_slope = abs(last_ema) * min_slope_frac
    if abs(slope) < min_abs_slope:
        return "flat"
    if last_price > last_ema and slope > 0:
        return "up"
    if last_price < last_ema and slope < 0:
        return "down"
    return "flat"


# ──────────────────────────────────────────────────────────────────────
# Swing detection (fractals)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Swing:
    """A single swing point: index in the input series and its price."""

    index: int
    price: float
    kind: Literal["high", "low"]


def swing_points(
    highs: Sequence[float], lows: Sequence[float], fractal_n: int = 2
) -> list[Swing]:
    """Fractal-based swing detection. A swing high at index i is a `high[i]`
    that is **strictly greater** than every high in `[i-fractal_n, i+fractal_n]`
    excluding itself. Symmetric for swing lows.

    Returns swings in chronological order, mixed highs and lows.
    The last `fractal_n` bars can never be classified yet (need future bars)
    so they're skipped — caller can treat the most recent unfilled stretch as
    "live"."""
    h = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    n = len(h)
    out: list[Swing] = []
    if n < 2 * fractal_n + 1:
        return out

    for i in range(fractal_n, n - fractal_n):
        window_h = h[i - fractal_n : i + fractal_n + 1]
        window_l = low[i - fractal_n : i + fractal_n + 1]
        center_h = window_h[fractal_n]
        center_l = window_l[fractal_n]
        if center_h == window_h.max() and (window_h == center_h).sum() == 1:
            out.append(Swing(i, float(center_h), "high"))
        # A bar can be a swing high AND a swing low (rare; doji at extreme) —
        # we test both independently.
        if center_l == window_l.min() and (window_l == center_l).sum() == 1:
            out.append(Swing(i, float(center_l), "low"))
    return out


# ──────────────────────────────────────────────────────────────────────
# Break of Structure (BOS) and last impulse leg
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BOS:
    """A confirmed Break of Structure.

    `leg_low` / `leg_high` describe the impulse that caused the break:
      - bullish BOS: leg from the prior swing low up through the broken high
      - bearish BOS: leg from the prior swing high down through the broken low
    """

    direction: Literal["up", "down"]
    broken_swing_index: int
    broken_swing_price: float
    leg_low: float
    leg_high: float


def last_bos(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
    fractal_n: int = 2,
) -> Optional[BOS]:
    """Find the most recent confirmed Break of Structure.

    A BOS is confirmed when the current bar's CLOSE breaches the most recent
    opposite-kind swing point (bullish BOS: close > last swing high;
    bearish BOS: close < last swing low). The impulse leg is measured from
    the prior swing of the opposite kind."""
    swings = swing_points(highs, lows, fractal_n)
    if len(swings) < 2:
        return None
    c = np.asarray(closes, dtype=float)
    cur_close = float(c[-1])

    # A BOS is the current close breaching the most recent swing high
    # (bullish) or low (bearish). The impulse leg is measured from the most
    # recent OPPOSITE swing that came *before* the broken swing. This handles
    # the common "high → pullback low → break of that high" pattern.
    highs_list = [s for s in swings if s.kind == "high"]
    lows_list = [s for s in swings if s.kind == "low"]
    if not highs_list or not lows_list:
        return None

    last_high = highs_list[-1]
    last_low = lows_list[-1]

    if cur_close > last_high.price:
        prior_lows = [s for s in lows_list if s.index < last_high.index]
        if prior_lows:
            leg_low = prior_lows[-1].price
            return BOS(
                direction="up",
                broken_swing_index=last_high.index,
                broken_swing_price=last_high.price,
                leg_low=leg_low,
                leg_high=last_high.price,
            )

    if cur_close < last_low.price:
        prior_highs = [s for s in highs_list if s.index < last_low.index]
        if prior_highs:
            leg_high = prior_highs[-1].price
            return BOS(
                direction="down",
                broken_swing_index=last_low.index,
                broken_swing_price=last_low.price,
                leg_low=last_low.price,
                leg_high=leg_high,
            )

    return None


# ──────────────────────────────────────────────────────────────────────
# Optimal Trade Entry (OTE) zone — Fib 62-79% retracement
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    side: Literal["bullish", "bearish"]

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def ote_zone(leg_low: float, leg_high: float, direction: Literal["up", "down"]) -> Zone:
    """OTE zone = Fib 62-79% retracement of the impulse leg.

    Bullish leg (low → high): zone retraces from the high back down.
        zone_top = high - 0.62 * (high - low)
        zone_bot = high - 0.79 * (high - low)

    Bearish leg (high → low): zone retraces from the low back up.
        zone_bot = low + 0.62 * (high - low)
        zone_top = low + 0.79 * (high - low)
    """
    rng = leg_high - leg_low
    if direction == "up":
        top = leg_high - 0.62 * rng
        bot = leg_high - 0.79 * rng
        return Zone(low=bot, high=top, side="bullish")
    bot = leg_low + 0.62 * rng
    top = leg_low + 0.79 * rng
    return Zone(low=bot, high=top, side="bearish")


# ──────────────────────────────────────────────────────────────────────
# Fair Value Gaps (3-candle imbalance)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FVG:
    index: int                              # index of the third candle
    low: float
    high: float
    side: Literal["bullish", "bearish"]
    mitigated: bool

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def fair_value_gaps(
    highs: Sequence[float], lows: Sequence[float],
) -> list[FVG]:
    """Find all 3-candle imbalances.

    Bullish FVG at index i (i >= 2):  low[i]  > high[i-2]
        → gap = (high[i-2], low[i])
    Bearish FVG at index i (i >= 2):  high[i] < low[i-2]
        → gap = (high[i], low[i-2])

    A gap is "mitigated" if any subsequent candle has wicked through it
    (intersected the gap range). Returns gaps in chronological order."""
    h = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    n = len(h)
    gaps: list[FVG] = []
    for i in range(2, n):
        if low[i] > h[i - 2]:
            gap_low, gap_high = float(h[i - 2]), float(low[i])
            mitigated = bool((low[i + 1 :] < gap_high).any() and (h[i + 1 :] > gap_low).any())
            # Stronger check: any later candle whose [low,high] overlaps the gap.
            later_lows = low[i + 1 :]
            later_highs = h[i + 1 :]
            mitigated = bool(np.any((later_lows <= gap_high) & (later_highs >= gap_low)))
            gaps.append(FVG(i, gap_low, gap_high, "bullish", mitigated))
        elif h[i] < low[i - 2]:
            gap_low, gap_high = float(h[i]), float(low[i - 2])
            later_lows = low[i + 1 :]
            later_highs = h[i + 1 :]
            mitigated = bool(np.any((later_lows <= gap_high) & (later_highs >= gap_low)))
            gaps.append(FVG(i, gap_low, gap_high, "bearish", mitigated))
    return gaps


# ──────────────────────────────────────────────────────────────────────
# Horizontal level clustering
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LevelZone:
    """A cluster of nearby horizontal levels treated as one zone."""

    low: float
    high: float
    sources: int   # how many raw levels collapsed into this zone

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


def cluster_levels(prices: Sequence[float], tolerance_pct: float = 0.2) -> list[LevelZone]:
    """Cluster prices that are within `tolerance_pct` of each other into zones.

    `tolerance_pct` is a percentage of the level price (e.g. 0.2 = 0.2%).
    Returns zones sorted ascending by midpoint."""
    if not prices:
        return []
    sorted_p = sorted(float(p) for p in prices)
    zones: list[LevelZone] = []
    cur = [sorted_p[0]]
    for p in sorted_p[1:]:
        threshold = cur[-1] * (tolerance_pct / 100.0)
        if p - cur[-1] <= threshold:
            cur.append(p)
        else:
            zones.append(LevelZone(min(cur), max(cur), len(cur)))
            cur = [p]
    zones.append(LevelZone(min(cur), max(cur), len(cur)))
    return zones


# ──────────────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────────────


def round_levels_near(price: float, step: float, count: int = 3) -> list[float]:
    """Return the `count` nearest round-number levels to `price` at multiples
    of `step`. E.g. price=69658, step=1000 → [68000, 69000, 70000]."""
    base = round(price / step) * step
    return sorted({base + i * step for i in range(-count, count + 1)})
