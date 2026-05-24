"""Tests for cero/brain/criteria.py.

Crafted MarketContexts — no exchange, no DB. Each test builds the minimum
candle history needed for one criterion and asserts pass/fail behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from cero.brain.criteria import (
    ALL_CRITERIA,
    MarketContext,
    atr_room,
    evaluate_all,
    key_levels,
    ltf_poi,
    market_structure,
    poi_alert,
    session_hl,
    structure_15m_30m,
    trend_h1_h4,
)
from cero.config import CriteriaWeights
from cero.data.exchange import Candle


# ──────────────────────────────────────────────────────────────────────
# Helpers — synthetic candle generators
# ──────────────────────────────────────────────────────────────────────


# fixed weights for tests (sums to 100)
WEIGHTS = CriteriaWeights(
    trend_h1_h4=20,
    market_structure=18,
    key_levels=10,
    poi_alert=15,
    session_hl=5,
    structure_15m_30m=12,
    ltf_poi=12,
    atr_room=8,
)

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def make_candles(
    closes: list[float],
    timeframe: str,
    *,
    end_ms: int | None = None,
    spread: float = 0.5,
    symbol: str = "BTC/USDT:USDT",
) -> list[Candle]:
    """Build N candles aligned to `timeframe`, ending at `end_ms` (default: now).
    Each candle's high/low extend `spread` above/below the close."""
    step = TF_MS[timeframe]
    if end_ms is None:
        end_ms = int(datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    candles = []
    n = len(closes)
    first_open = end_ms - (n - 1) * step
    for i, c in enumerate(closes):
        ot = first_open + i * step
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=ot,
                open=c,
                high=c + spread,
                low=c - spread,
                close=c,
                volume=100.0,
            )
        )
    return candles


def base_ctx(candles_by_tf: dict[str, list[Candle]]) -> MarketContext:
    now_ms = int(datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return MarketContext(
        symbol="BTC/USDT:USDT",
        now_ms=now_ms,
        candles=candles_by_tf,
        weights=WEIGHTS,
        round_step=1000.0,
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 1 — trend_h1_h4
# ──────────────────────────────────────────────────────────────────────


def test_c1_passes_when_h1_and_h4_both_up():
    rising = list(np.linspace(100.0, 200.0, 100))
    ctx = base_ctx({"1h": make_candles(rising, "1h"), "4h": make_candles(rising, "4h")})
    r = trend_h1_h4(ctx)
    assert r.passed is True
    assert r.direction_hint == "up"


def test_c1_fails_when_directions_disagree():
    rising = list(np.linspace(100.0, 200.0, 100))
    falling = list(np.linspace(200.0, 100.0, 100))
    ctx = base_ctx({"1h": make_candles(rising, "1h"), "4h": make_candles(falling, "4h")})
    r = trend_h1_h4(ctx)
    assert r.passed is False
    assert r.direction_hint is None


def test_c1_fails_when_history_too_short():
    short = [100.0] * 30
    ctx = base_ctx({"1h": make_candles(short, "1h"), "4h": make_candles(short, "4h")})
    r = trend_h1_h4(ctx)
    assert r.passed is False


# ──────────────────────────────────────────────────────────────────────
# Criterion 2 — market_structure (BOS aligned with HTF)
# ──────────────────────────────────────────────────────────────────────


def test_c2_passes_on_bullish_bos_with_uptrend():
    # For a bullish BOS the structure must be: swing-LOW → swing-HIGH → close
    # above that swing-high. Build closes with that exact order so the fractal
    # detector picks them up unambiguously.
    base = list(np.linspace(100, 150, 70))     # rising to set up uptrend
    swing_low = [150, 145, 140, 145, 150]      # trough at 140
    swing_high = [150, 160, 170, 165, 160]     # peak at 170 (after the low)
    impulse = [165, 175, 180]                  # close 180 > 170 → bullish BOS
    closes = base + swing_low + swing_high + impulse
    candles_1h = make_candles(closes, "1h", spread=0.1)
    candles_4h = make_candles(list(np.linspace(100, 200, 100)), "4h")
    ctx = base_ctx({"1h": candles_1h, "4h": candles_4h})
    r = market_structure(ctx)
    assert r.passed is True, r.detail
    assert r.direction_hint == "up"


def test_c2_fails_when_htf_is_flat():
    flat = [100.0] * 100
    ctx = base_ctx({"1h": make_candles(flat, "1h"), "4h": make_candles(flat, "4h")})
    r = market_structure(ctx)
    assert r.passed is False


# ──────────────────────────────────────────────────────────────────────
# Criterion 8 — atr_room
# ──────────────────────────────────────────────────────────────────────


def test_c8_passes_when_today_range_below_h4_atr():
    # 4h candles with steady 200-point range each → ATR ~200
    n = 30
    c4 = []
    spread = 100.0  # high - low = 200
    closes_4h = [50000.0] * n
    c4 = make_candles(closes_4h, "4h", spread=spread)
    # Today's 1d candle with a small range of 50 (well under 200)
    today_ms = int(datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    today_d = Candle(
        symbol="BTC/USDT:USDT",
        timeframe="1d",
        open_time=today_ms,
        open=50000.0,
        high=50025.0,
        low=49975.0,
        close=50000.0,
        volume=100.0,
    )
    ctx = base_ctx({"4h": c4, "1d": [today_d]})
    r = atr_room(ctx)
    assert r.passed is True


def test_c8_fails_when_today_range_above_h4_atr():
    # ATR ~50 from 4h candles, today's range = 500 → should fail
    closes_4h = [50000.0] * 30
    c4 = make_candles(closes_4h, "4h", spread=25.0)  # range 50
    today_ms = int(datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    today_d = Candle(
        symbol="BTC/USDT:USDT",
        timeframe="1d",
        open_time=today_ms,
        open=50000.0,
        high=50250.0,
        low=49750.0,
        close=50000.0,
        volume=100.0,
    )
    ctx = base_ctx({"4h": c4, "1d": [today_d]})
    r = atr_room(ctx)
    assert r.passed is False


# ──────────────────────────────────────────────────────────────────────
# Criterion 3 — key_levels
# ──────────────────────────────────────────────────────────────────────


def test_c3_finds_levels_when_swings_exist():
    # Mix of swing highs/lows around the current price → should find zones
    closes = list(np.linspace(70000, 72000, 50)) + list(np.linspace(72000, 70000, 50))
    c1h = make_candles(closes, "1h", spread=20.0)
    ctx = base_ctx({"1h": c1h, "4h": c1h, "1d": c1h[-5:]})
    r = key_levels(ctx)
    # We may or may not find a zone within 1 x ATR depending on geometry — just
    # check the result is well-formed and reports some zones.
    assert r.meta["zones_total"] >= 1


# ──────────────────────────────────────────────────────────────────────
# evaluate_all sanity
# ──────────────────────────────────────────────────────────────────────


def test_evaluate_all_returns_one_result_per_criterion():
    rising = list(np.linspace(100, 200, 100))
    ctx = base_ctx({"1h": make_candles(rising, "1h"), "4h": make_candles(rising, "4h")})
    results = evaluate_all(ctx)
    assert len(results) == len(ALL_CRITERIA)
    # Names match the order in the registry
    assert [r.name for r in results] == [
        "trend_h1_h4", "market_structure", "key_levels", "poi_alert",
        "session_hl", "structure_15m_30m", "ltf_poi", "atr_room",
    ]
    # Weights match config
    assert sum(r.weight for r in results) == 100
    # Score is weight when passed, 0 otherwise
    for r in results:
        assert r.score == (r.weight if r.passed else 0)


# ──────────────────────────────────────────────────────────────────────
# Edge cases — missing data should fail gracefully, not crash
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fn", ALL_CRITERIA)
def test_each_criterion_handles_empty_context(fn):
    """Every criterion must fail-safe (return passed=False) on empty data,
    never raise."""
    # `current_price` raises if all TFs are empty — supply one bar in 1m so
    # the property works; individual criteria should still report fail.
    bar = Candle(
        symbol="BTC/USDT:USDT", timeframe="1m", open_time=0,
        open=100, high=100, low=100, close=100, volume=0,
    )
    ctx = base_ctx({"1m": [bar]})
    r = fn(ctx)
    assert r.passed is False
    assert isinstance(r.detail, str) and r.detail
