"""Unit tests for cero/brain/indicators.py.

Synthetic data only — no exchange, no DB. Verifies the math against hand-
computed expected values for the standard formulas (EMA, ATR Wilder, swings,
BOS, FVG, OTE).
"""
from __future__ import annotations

import numpy as np
import pytest

from cero.brain.indicators import (
    atr,
    classify_trend,
    cluster_levels,
    ema,
    fair_value_gaps,
    last_bos,
    ote_zone,
    round_levels_near,
    swing_points,
)


# ──────────────────────────────────────────────────────────────────────
# EMA
# ──────────────────────────────────────────────────────────────────────


def test_ema_first_period_minus_one_are_nan():
    values = list(range(1, 11))   # 1..10
    out = ema(values, period=3)
    assert np.isnan(out[0])
    assert np.isnan(out[1])
    # SMA seed at index 2: mean(1,2,3) = 2.0
    assert out[2] == pytest.approx(2.0)


def test_ema_recursion_uses_smoothed_alpha():
    # period=3 → alpha = 2/4 = 0.5
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema(values, period=3)
    # out[2] = 2.0 (SMA seed); out[3] = 0.5*4 + 0.5*2 = 3.0; out[4] = 0.5*5+0.5*3 = 4.0
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_constant_series_is_constant():
    out = ema([7.0] * 20, period=5)
    assert out[4] == pytest.approx(7.0)
    assert out[-1] == pytest.approx(7.0)


# ──────────────────────────────────────────────────────────────────────
# ATR
# ──────────────────────────────────────────────────────────────────────


def test_atr_constant_range_equals_range():
    # Every bar has the same true range of 2.0 → ATR should converge to 2.0
    n = 30
    h = np.full(n, 102.0)
    l = np.full(n, 100.0)
    c = np.full(n, 101.0)
    out = atr(h, l, c, period=14)
    # First 14 are NaN, index 14 is the seed (mean of first 14 TRs), should be 2.0
    assert out[14] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(2.0)


def test_atr_returns_nan_when_insufficient_data():
    out = atr([1, 2], [0.5, 1.5], [1, 1], period=14)
    assert np.all(np.isnan(out))


# ──────────────────────────────────────────────────────────────────────
# classify_trend
# ──────────────────────────────────────────────────────────────────────


def test_classify_trend_up_on_monotonic_rise():
    closes = list(np.linspace(100, 200, 100))   # straight uptrend
    assert classify_trend(closes) == "up"


def test_classify_trend_down_on_monotonic_fall():
    closes = list(np.linspace(200, 100, 100))
    assert classify_trend(closes) == "down"


def test_classify_trend_flat_on_choppy_data():
    rng = np.random.default_rng(42)
    closes = 100 + rng.normal(0, 0.5, size=200)  # tight random walk
    # With a tight random walk, the slope is near zero — should be flat.
    assert classify_trend(closes.tolist()) == "flat"


def test_classify_trend_flat_with_insufficient_history():
    assert classify_trend([1.0] * 10) == "flat"


# ──────────────────────────────────────────────────────────────────────
# Swing points
# ──────────────────────────────────────────────────────────────────────


def test_swing_points_finds_obvious_pivot():
    # A clear M-shape: high in the middle
    highs = [1, 2, 3, 4, 5, 4, 3, 2, 1]
    lows = [0, 1, 2, 3, 4, 3, 2, 1, 0]
    swings = swing_points(highs, lows, fractal_n=2)
    kinds = [s.kind for s in swings]
    # Should find at least one swing high at index 4 (price 5)
    highs_found = [s for s in swings if s.kind == "high"]
    assert any(s.index == 4 and s.price == 5 for s in highs_found)


def test_swing_points_empty_when_too_short():
    assert swing_points([1, 2, 3], [1, 1, 1], fractal_n=2) == []


# ──────────────────────────────────────────────────────────────────────
# BOS
# ──────────────────────────────────────────────────────────────────────


def test_last_bos_detects_bullish_break():
    # Build: low at idx 2, high at idx 6, then a close above the high at the end
    highs = [10, 11, 10, 11, 12, 11, 13, 12, 12, 12, 12, 12, 12, 12, 12]
    lows = [9, 10, 8, 10, 11, 10, 12, 11, 11, 11, 11, 11, 11, 11, 11]
    closes = [10, 10, 9, 11, 11, 11, 12, 12, 12, 12, 12, 12, 12, 12, 14]  # last close = 14 > 13
    bos = last_bos(highs, lows, closes, fractal_n=2)
    assert bos is not None
    assert bos.direction == "up"
    assert bos.broken_swing_price == 13


def test_last_bos_returns_none_when_no_break():
    n = 30
    highs = list(np.full(n, 100.0))
    lows = list(np.full(n, 99.0))
    closes = list(np.full(n, 99.5))
    assert last_bos(highs, lows, closes) is None


# ──────────────────────────────────────────────────────────────────────
# OTE zone
# ──────────────────────────────────────────────────────────────────────


def test_ote_zone_bullish_is_in_upper_retracement():
    z = ote_zone(leg_low=100.0, leg_high=200.0, direction="up")
    # 100 leg, 62%-79% retrace from high = 138 down to 121
    assert z.low == pytest.approx(121.0)
    assert z.high == pytest.approx(138.0)
    assert z.side == "bullish"
    assert z.contains(130.0)
    assert not z.contains(150.0)


def test_ote_zone_bearish_is_mirrored():
    z = ote_zone(leg_low=100.0, leg_high=200.0, direction="down")
    # 62%-79% retrace from low = 162 to 179
    assert z.low == pytest.approx(162.0)
    assert z.high == pytest.approx(179.0)
    assert z.side == "bearish"


# ──────────────────────────────────────────────────────────────────────
# FVG
# ──────────────────────────────────────────────────────────────────────


def test_fvg_finds_bullish_gap_and_marks_unmitigated():
    # Three candles: candle 2's low > candle 0's high → bullish gap (high0, low2)
    highs = [10, 12, 15]
    lows = [8, 10, 13]   # low[2]=13 > high[0]=10  → gap (10, 13)
    gaps = fair_value_gaps(highs, lows)
    assert len(gaps) == 1
    assert gaps[0].side == "bullish"
    assert gaps[0].low == 10
    assert gaps[0].high == 13
    assert gaps[0].mitigated is False


def test_fvg_marks_mitigated_when_later_candle_intersects():
    highs = [10, 12, 15, 14, 11]
    lows = [8, 10, 13, 9, 9]      # candle 3 has low=9 < gap_low=10? Actually overlap (9..14) overlaps (10..13)
    gaps = fair_value_gaps(highs, lows)
    bull = [g for g in gaps if g.side == "bullish"]
    assert bull and bull[0].mitigated is True


# ──────────────────────────────────────────────────────────────────────
# Level clustering & round numbers
# ──────────────────────────────────────────────────────────────────────


def test_cluster_levels_merges_within_tolerance():
    # 100.0 and 100.1 are 0.1% apart, tolerance 0.2% → merged
    # 105.0 is far away → separate zone
    zones = cluster_levels([100.0, 100.1, 105.0], tolerance_pct=0.2)
    assert len(zones) == 2
    assert zones[0].low == 100.0
    assert zones[0].high == 100.1
    assert zones[0].sources == 2


def test_round_levels_near_btc_thousands():
    out = round_levels_near(price=69658.0, step=1000.0, count=2)
    # Nearest base = 70000; ±2 = 68000..72000
    assert out == [68000.0, 69000.0, 70000.0, 71000.0, 72000.0]
