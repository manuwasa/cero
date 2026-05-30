"""Mean-reversion strategy — the opposite hypothesis to SMC trend.

Premise: in choppy / range-bound markets (which testnet often is and
which alts often are between major trends), price tends to **revert**
from session extremes back to the mean, not break through them.

Setup (long):
  - Price within 0.5 × ATR(H1) of today's session LOW
  - H1 trend NOT strongly down (slope > -1%)
  - 1h candle showed a wick rejection (low < open AND close > open by N% range)

Setup (short): symmetric — within 0.5 × ATR of session HIGH, trend not strongly up,
  candle showed upper-wick rejection.

Entry: current price. SL: just past the session extreme. TP: midway to opposite extreme.
This produces ~1:1 to 1:1.5 RR, smaller than SMC's 2:1, but expected win rate is
higher (55-65% in range markets).

NOTE: this strategy is **shadow-mode** for now. It collects signals into the DB
tagged with strategy="mean_reversion" but doesn't trade. After 1-2 weeks of live
data we compare against smc_trend and decide which (or both) is worth running.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from cero.brain.indicators import atr, classify_trend
from cero.brain.signals import Signal
from cero.brain.strategies.base import Strategy, StrategyContext


_SESSION_PROXIMITY = 0.5      # within 0.5 × ATR(H1) of session extreme
_REJECTION_WICK_FRAC = 0.4    # wick must be >40% of candle range
_TARGET_FRAC = 0.5            # take profit at 50% of session range from entry


def _today_utc(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()


class MeanReversionStrategy:
    """Fade extremes at session H/L when there's wick rejection + no strong
    HTF trend against the fade."""

    name: str = "mean_reversion"

    async def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        market = ctx.market
        candles_1h = market.candles.get("1h") or []
        candles_15m = market.candles.get("15m") or []

        # Need at least 20 1h candles for ATR + trend + session calc.
        if len(candles_1h) < 20 or ctx.atr_h1 <= 0:
            return None

        # Today's session H/L from 15m intra-day bars (more granular than 1h).
        today = _today_utc(market.now_ms)
        today_bars = [
            c for c in candles_15m
            if _today_utc(c.open_time) == today
        ]
        if len(today_bars) < 4:
            return None    # not enough data yet today
        session_high = max(c.high for c in today_bars)
        session_low = min(c.low for c in today_bars)
        session_range = session_high - session_low
        if session_range < 0.5 * ctx.atr_h1:
            return None    # range too small to fade

        price = market.current_price
        atr_h1 = ctx.atr_h1
        last_1h = candles_1h[-1]

        # Detect upper-wick rejection (potential short setup):
        # candle range = high - low, upper wick = high - max(open, close)
        c = last_1h
        rng = max(c.high - c.low, 1e-9)
        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low
        upper_rejection = (upper_wick / rng) >= _REJECTION_WICK_FRAC
        lower_rejection = (lower_wick / rng) >= _REJECTION_WICK_FRAC

        # Trend strength check — don't fade with a strong opposite trend.
        # We use the same trend classifier but ignore slope direction for
        # "weak/flat" judgment.
        closes = np.array([x.close for x in candles_1h])
        trend = classify_trend(closes.tolist())

        direction: str
        if (
            abs(price - session_high) <= _SESSION_PROXIMITY * atr_h1
            and upper_rejection
            and trend != "up"
        ):
            direction = "short"
            entry = price
            sl = session_high + 0.2 * atr_h1
            tp = price - _TARGET_FRAC * session_range
        elif (
            abs(price - session_low) <= _SESSION_PROXIMITY * atr_h1
            and lower_rejection
            and trend != "down"
        ):
            direction = "long"
            entry = price
            sl = session_low - 0.2 * atr_h1
            tp = price + _TARGET_FRAC * session_range
        else:
            return None    # no setup

        stop_distance = abs(entry - sl)
        if stop_distance <= 0:
            return None

        # Mean reversion doesn't use the 8-criteria scoring. We give it a
        # synthetic tier+score so it slots into the same Signal shape:
        #  - tier 'B' (treats it as actionable)
        #  - score 65 (above the default B threshold so it gets dispatched)
        # In auto/approval mode, only signals with strategy == cfg.primary_strategy
        # are acted on, so unless you flip primary to 'mean_reversion' this is
        # shadow data only.
        decision = ctx.risk_gate.size_for(
            equity=ctx.equity,
            tier_multiplier=0.5,    # half-size while we're testing this strategy
            stop_distance=stop_distance,
            open_positions=ctx.open_positions,
            today_realized=ctx.today_realized,
            today_consecutive_losses=ctx.today_consecutive_losses,
            in_blackout=ctx.in_blackout,
            blackout_name=ctx.blackout_name,
        )

        return Signal(
            ts=market.now_ms,
            symbol=market.symbol,
            tier="B",
            direction=direction,
            score=65,
            size_multiplier=0.5,
            size=decision.size,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            mode=ctx.mode,
            strategy=self.name,
            criteria_json=json.dumps([{
                "strategy": "mean_reversion",
                "setup": "upper_rejection" if direction == "short" else "lower_rejection",
                "session_high": session_high,
                "session_low": session_low,
                "session_range": session_range,
                "atr_h1": atr_h1,
                "trend": trend,
            }]),
            size_reason=decision.reason,
            notes=None,
        )
