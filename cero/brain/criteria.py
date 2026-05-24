"""
The 8 scoring criteria.

PURE FUNCTIONS. No I/O, no DB, no exchange. Each takes a frozen MarketContext
and returns a CriterionResult. The brain wires these together via evaluate_all.

See docs/CRITERIA.md for the full spec of each criterion — this module is the
direct implementation of that doc. If the math here ever drifts from the doc,
treat the doc as truth and fix the code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

import numpy as np

from cero.brain.indicators import (
    BOS,
    Trend,
    Zone,
    atr,
    classify_trend,
    cluster_levels,
    fair_value_gaps,
    last_bos,
    ote_zone,
    round_levels_near,
    swing_points,
)
from cero.config import CriteriaWeights
from cero.data.exchange import Candle

Direction = Literal["up", "down"]

# ──────────────────────────────────────────────────────────────────────
# Context + result types
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarketContext:
    """Everything a criterion needs to evaluate a symbol. Candles per timeframe
    are passed sorted oldest → newest. Build via `MarketContext.build(...)`
    when you need to assemble from a dict of timeframes."""

    symbol: str
    now_ms: int
    candles: dict[str, list[Candle]]
    weights: CriteriaWeights
    # Round-number step for this symbol in quote units. BTC=1000, ETH=100,
    # SOL=10. Used by criterion 3 (key levels).
    round_step: float = 1000.0

    @property
    def current_price(self) -> float:
        """Latest close from the smallest available timeframe. Falls back to
        any timeframe with data."""
        for tf in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"):
            cs = self.candles.get(tf)
            if cs:
                return cs[-1].close
        raise ValueError(f"MarketContext({self.symbol}): no candles in any timeframe")


@dataclass(frozen=True)
class CriterionResult:
    name: str
    weight: int
    passed: bool
    detail: str
    direction_hint: Optional[Direction] = None
    # Extra structured info for the dashboard — e.g. {"htf_trend": "up"}.
    meta: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        return self.weight if self.passed else 0


# ──────────────────────────────────────────────────────────────────────
# Helpers used by multiple criteria
# ──────────────────────────────────────────────────────────────────────


def _closes(cs: list[Candle]) -> np.ndarray:
    return np.array([c.close for c in cs], dtype=float)


def _highs(cs: list[Candle]) -> np.ndarray:
    return np.array([c.high for c in cs], dtype=float)


def _lows(cs: list[Candle]) -> np.ndarray:
    return np.array([c.low for c in cs], dtype=float)


def _atr_last(cs: list[Candle], period: int = 14) -> Optional[float]:
    if len(cs) < period + 1:
        return None
    a = atr(_highs(cs), _lows(cs), _closes(cs), period)
    last = a[-1]
    return None if np.isnan(last) else float(last)


def _today_utc_date(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()


def _session_high_low(
    cs: list[Candle], now_ms: int
) -> Optional[tuple[float, float]]:
    """Return (session_high, session_low) for today's UTC date based on the
    given candles, or None if there are no candles for today yet."""
    today = _today_utc_date(now_ms)
    today_bars = [
        c for c in cs
        if datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc).date().isoformat() == today
    ]
    if not today_bars:
        return None
    return max(c.high for c in today_bars), min(c.low for c in today_bars)


def _htf_trend(ctx: MarketContext) -> tuple[Trend, Trend]:
    """Convenience: (1h_trend, 4h_trend) used by multiple criteria."""
    c1 = ctx.candles.get("1h") or []
    c4 = ctx.candles.get("4h") or []
    t1 = classify_trend(_closes(c1)) if len(c1) >= 55 else "flat"
    t4 = classify_trend(_closes(c4)) if len(c4) >= 55 else "flat"
    return t1, t4


# ──────────────────────────────────────────────────────────────────────
# Criterion 1 — HTF trend (H1 + H4)
# ──────────────────────────────────────────────────────────────────────


def trend_h1_h4(ctx: MarketContext) -> CriterionResult:
    t1, t4 = _htf_trend(ctx)
    passed = t1 in ("up", "down") and t1 == t4
    direction: Optional[Direction] = t1 if passed else None  # type: ignore[assignment]
    return CriterionResult(
        name="trend_h1_h4",
        weight=ctx.weights.trend_h1_h4,
        passed=passed,
        detail=f"H1={t1}, H4={t4}",
        direction_hint=direction,
        meta={"h1_trend": t1, "h4_trend": t4},
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 2 — clean market structure (1h BOS aligned with HTF trend)
# ──────────────────────────────────────────────────────────────────────


def market_structure(ctx: MarketContext) -> CriterionResult:
    htf, _ = _htf_trend(ctx)
    c1h = ctx.candles.get("1h") or []
    if len(c1h) < 20 or htf == "flat":
        return CriterionResult(
            name="market_structure",
            weight=ctx.weights.market_structure,
            passed=False,
            detail=f"htf={htf}, 1h_bars={len(c1h)} (need 20)",
        )
    bos = last_bos(_highs(c1h), _lows(c1h), _closes(c1h))
    if bos is None:
        return CriterionResult(
            name="market_structure",
            weight=ctx.weights.market_structure,
            passed=False,
            detail="no BOS detected on 1h",
            meta={"htf_trend": htf},
        )
    passed = bos.direction == htf
    return CriterionResult(
        name="market_structure",
        weight=ctx.weights.market_structure,
        passed=passed,
        detail=f"BOS={bos.direction} vs HTF={htf}",
        direction_hint=bos.direction if passed else None,
        meta={
            "bos_direction": bos.direction,
            "bos_leg_low": bos.leg_low,
            "bos_leg_high": bos.leg_high,
        },
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 3 — key levels marked
# ──────────────────────────────────────────────────────────────────────


def key_levels(ctx: MarketContext) -> CriterionResult:
    c1h = ctx.candles.get("1h") or []
    c4h = ctx.candles.get("4h") or []
    c1d = ctx.candles.get("1d") or []
    a = _atr_last(c1h)
    price = ctx.current_price

    if a is None or not c1h:
        return CriterionResult(
            name="key_levels",
            weight=ctx.weights.key_levels,
            passed=False,
            detail="insufficient 1h history for ATR",
        )

    # Gather raw horizontal levels.
    raw: list[float] = []
    for cs in (c1h, c4h, c1d):
        for s in swing_points(_highs(cs), _lows(cs)):
            raw.append(s.price)
    if c1d:
        raw.append(c1d[-1].high)
        raw.append(c1d[-1].low)
    raw.extend(round_levels_near(price, ctx.round_step, count=2))

    zones = cluster_levels(raw, tolerance_pct=0.2)
    near = [z for z in zones if abs(z.mid - price) <= a]
    passed = len(near) >= 1 and len(zones) >= 2
    return CriterionResult(
        name="key_levels",
        weight=ctx.weights.key_levels,
        passed=passed,
        detail=f"{len(zones)} zones total, {len(near)} within 1xATR ({a:.2f})",
        meta={"zones_total": len(zones), "zones_near": len(near), "atr_h1": a},
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 4 — alert on POI (OTE zone or unmitigated FVG)
# ──────────────────────────────────────────────────────────────────────


def poi_alert(ctx: MarketContext) -> CriterionResult:
    c1h = ctx.candles.get("1h") or []
    htf, _ = _htf_trend(ctx)
    if len(c1h) < 20 or htf == "flat":
        return CriterionResult(
            name="poi_alert",
            weight=ctx.weights.poi_alert,
            passed=False,
            detail=f"htf={htf}, 1h_bars={len(c1h)}",
        )

    bos = last_bos(_highs(c1h), _lows(c1h), _closes(c1h))
    price = ctx.current_price
    in_ote = False
    in_fvg = False

    if bos is not None and bos.direction == htf:
        zone = ote_zone(bos.leg_low, bos.leg_high, bos.direction)
        in_ote = zone.contains(price)

    # Unmitigated FVGs on 1h aligned with HTF.
    fvgs = fair_value_gaps(_highs(c1h), _lows(c1h))
    want_side = "bullish" if htf == "up" else "bearish"
    aligned_fvgs = [g for g in fvgs if g.side == want_side and not g.mitigated]
    in_fvg = any(g.contains(price) for g in aligned_fvgs)

    passed = in_ote or in_fvg
    return CriterionResult(
        name="poi_alert",
        weight=ctx.weights.poi_alert,
        passed=passed,
        detail=f"in_OTE={in_ote}, in_unmitigated_FVG={in_fvg}",
        direction_hint=htf if passed else None,  # type: ignore[arg-type]
        meta={"htf_trend": htf, "in_ote": in_ote, "in_fvg": in_fvg},
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 5 — session high/low marked + reacted-to
# ──────────────────────────────────────────────────────────────────────


def session_hl(ctx: MarketContext) -> CriterionResult:
    # Prefer 15m for session intra-day resolution; fall back to 1h.
    cs = ctx.candles.get("15m") or ctx.candles.get("1h") or []
    c1h = ctx.candles.get("1h") or []
    a = _atr_last(c1h)
    if not cs or a is None:
        return CriterionResult(
            name="session_hl",
            weight=ctx.weights.session_hl,
            passed=False,
            detail="insufficient intra-day data",
        )

    hl = _session_high_low(cs, ctx.now_ms)
    if hl is None:
        return CriterionResult(
            name="session_hl",
            weight=ctx.weights.session_hl,
            passed=False,
            detail="no candles for today's UTC date",
        )
    sh, sl = hl
    if sh - sl < 0.5 * a:
        return CriterionResult(
            name="session_hl",
            weight=ctx.weights.session_hl,
            passed=False,
            detail=f"session range ({sh - sl:.2f}) < 0.5 x ATR ({a:.2f})",
        )

    # "Reacted recently": in the last 16 bars of this TF, price came within
    # 0.3 x ATR of session_h or session_l.
    lookback = cs[-16:]
    reacted_h = any(abs(c.high - sh) <= 0.3 * a for c in lookback)
    reacted_l = any(abs(c.low - sl) <= 0.3 * a for c in lookback)
    passed = reacted_h or reacted_l
    return CriterionResult(
        name="session_hl",
        weight=ctx.weights.session_hl,
        passed=passed,
        detail=f"session_h={sh:.2f}, session_l={sl:.2f}, reacted_h={reacted_h}, reacted_l={reacted_l}",
        meta={"session_high": sh, "session_low": sl},
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 6 — 15m/30m structure aligned with HTF
# ──────────────────────────────────────────────────────────────────────


def structure_15m_30m(ctx: MarketContext) -> CriterionResult:
    htf, _ = _htf_trend(ctx)
    if htf == "flat":
        return CriterionResult(
            name="structure_15m_30m",
            weight=ctx.weights.structure_15m_30m,
            passed=False,
            detail="HTF is flat",
        )

    matches: list[str] = []
    for tf in ("15m", "30m"):
        cs = ctx.candles.get(tf) or []
        if len(cs) < 55:
            continue
        ltf_trend = classify_trend(_closes(cs))
        if ltf_trend == htf:
            matches.append(f"{tf}={ltf_trend} (trend)")
            continue
        bos = last_bos(_highs(cs), _lows(cs), _closes(cs))
        if bos is not None and bos.direction == htf:
            matches.append(f"{tf}={bos.direction} (BOS)")

    passed = bool(matches)
    return CriterionResult(
        name="structure_15m_30m",
        weight=ctx.weights.structure_15m_30m,
        passed=passed,
        detail=", ".join(matches) if matches else f"no LTF agreement (htf={htf})",
        direction_hint=htf if passed else None,  # type: ignore[arg-type]
        meta={"htf_trend": htf, "ltf_matches": matches},
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 7 — LTF POI within 0.3 x ATR(H1) of price
# ──────────────────────────────────────────────────────────────────────


def ltf_poi(ctx: MarketContext) -> CriterionResult:
    c1h = ctx.candles.get("1h") or []
    a = _atr_last(c1h)
    htf, _ = _htf_trend(ctx)
    if a is None or htf == "flat":
        return CriterionResult(
            name="ltf_poi",
            weight=ctx.weights.ltf_poi,
            passed=False,
            detail=f"htf={htf}, atr_h1={a}",
        )

    price = ctx.current_price
    tolerance = 0.3 * a
    want_side = "bullish" if htf == "up" else "bearish"
    nearby = False
    found: list[str] = []

    for tf in ("5m", "15m"):
        cs = ctx.candles.get(tf) or []
        if len(cs) < 20:
            continue
        # OTE from last LTF BOS aligned with HTF.
        bos = last_bos(_highs(cs), _lows(cs), _closes(cs))
        if bos is not None and bos.direction == htf:
            z = ote_zone(bos.leg_low, bos.leg_high, bos.direction)
            if _zone_within(z, price, tolerance):
                nearby = True
                found.append(f"{tf}_OTE")
        # Unmitigated FVGs aligned with HTF.
        for g in fair_value_gaps(_highs(cs), _lows(cs)):
            if g.mitigated or g.side != want_side:
                continue
            if _fvg_within(g.low, g.high, price, tolerance):
                nearby = True
                found.append(f"{tf}_FVG")
                break

    return CriterionResult(
        name="ltf_poi",
        weight=ctx.weights.ltf_poi,
        passed=nearby,
        detail=", ".join(found) if found else "no LTF POI within 0.3 x ATR(H1)",
        meta={"htf_trend": htf, "tolerance": tolerance, "found": found},
    )


def _zone_within(z: Zone, price: float, tolerance: float) -> bool:
    if z.contains(price):
        return True
    nearest = min(abs(z.low - price), abs(z.high - price))
    return nearest <= tolerance


def _fvg_within(low: float, high: float, price: float, tolerance: float) -> bool:
    if low <= price <= high:
        return True
    nearest = min(abs(low - price), abs(high - price))
    return nearest <= tolerance


# ──────────────────────────────────────────────────────────────────────
# Criterion 8 — today's range < H4 ATR (room left)
# ──────────────────────────────────────────────────────────────────────


def atr_room(ctx: MarketContext) -> CriterionResult:
    c4h = ctx.candles.get("4h") or []
    if len(c4h) < 15:
        return CriterionResult(
            name="atr_room",
            weight=ctx.weights.atr_room,
            passed=False,
            detail=f"4h_bars={len(c4h)} (need 15)",
        )
    a4 = _atr_last(c4h)
    if a4 is None:
        return CriterionResult(
            name="atr_room",
            weight=ctx.weights.atr_room,
            passed=False,
            detail="ATR(14) on 4h unavailable",
        )

    # Today's range: prefer the 1d candle if today is in there, else compute
    # from intra-day candles.
    today = _today_utc_date(ctx.now_ms)
    c1d = ctx.candles.get("1d") or []
    today_range: Optional[float] = None
    if c1d:
        last_d = c1d[-1]
        if _today_utc_date(last_d.open_time) == today:
            today_range = last_d.high - last_d.low
    if today_range is None:
        # Compute from any intra-day TF that has bars today.
        for tf in ("1h", "30m", "15m", "5m"):
            cs = ctx.candles.get(tf) or []
            today_bars = [c for c in cs if _today_utc_date(c.open_time) == today]
            if today_bars:
                today_range = max(c.high for c in today_bars) - min(c.low for c in today_bars)
                break

    if today_range is None:
        return CriterionResult(
            name="atr_room",
            weight=ctx.weights.atr_room,
            passed=False,
            detail="no candles for today's UTC date",
        )

    passed = today_range < a4
    return CriterionResult(
        name="atr_room",
        weight=ctx.weights.atr_room,
        passed=passed,
        detail=f"today_range={today_range:.2f}, ATR4h={a4:.2f}",
        meta={"today_range": today_range, "atr_4h": a4},
    )


# ──────────────────────────────────────────────────────────────────────
# Registry & top-level entry point
# ──────────────────────────────────────────────────────────────────────


ALL_CRITERIA: list[Callable[[MarketContext], CriterionResult]] = [
    trend_h1_h4,
    market_structure,
    key_levels,
    poi_alert,
    session_hl,
    structure_15m_30m,
    ltf_poi,
    atr_room,
]


def evaluate_all(ctx: MarketContext) -> list[CriterionResult]:
    """Run every criterion in order, return results. The brain consumes this
    via `scoring.aggregate(results) → tier + direction`."""
    return [c(ctx) for c in ALL_CRITERIA]
