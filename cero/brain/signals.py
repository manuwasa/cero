"""
Signal emission.

A `Signal` is the artifact that crosses the brain → exec boundary. It bundles
*everything* the executor needs to act:
  - which side to take (long/short)
  - what size (already passed through the risk gate)
  - exact entry, stop loss, take profit prices
  - why (full criterion breakdown for the dashboard / Telegram message)

Build via `build_signal(ctx, report, equity, atr_h1)`; the function returns
None if the report isn't actionable or any required input is missing.

This module also persists Signals to the `signals` table and is responsible
for the change-detection that decides *when* to emit a new row (deferred to a
future step — `emit_if_changed` lives here but the live scheduler comes when
`cero/main.py` is wired).
"""
from __future__ import annotations

import json
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

from cero.brain.criteria import MarketContext
from cero.brain.risk import RiskGate
from cero.brain.scoring import ScoreReport
from cero.db.models import Signal as SignalRow
from cero.db.session import session_factory

Direction = Literal["long", "short", "none"]
Tier = Literal["A", "B", "C", "D"]


class Signal(BaseModel):
    """A fully-formed trade signal. Persists to the `signals` table 1:1."""

    ts: int
    symbol: str
    tier: Tier
    direction: Direction
    score: int
    size_multiplier: float       # tier sizing (1.0 / 0.5 / 0.0)
    size: float                  # final coin/contract count from risk.position_size
    entry_price: float
    stop_loss: float
    take_profit: float
    mode: str                    # 'signal_only' | 'approval' | 'auto'
    criteria_json: str = Field(default="[]")
    notes: Optional[str] = None
    # Reasoning for why we sized this trade as we did — surfaced in alerts.
    size_reason: str = "ok"

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def is_actionable(self) -> bool:
        return self.size > 0 and self.direction in ("long", "short")


# ──────────────────────────────────────────────────────────────────────
# Builder
# ──────────────────────────────────────────────────────────────────────


MIN_STOP_PCT = 0.003   # 0.3% — anything tighter is noise, gets stopped instantly
MAX_STOP_PCT = 0.030   # 3.0%  — anything wider is too much risk relative to entry


def _stop_and_target(
    direction: Direction, entry: float, atr_h1: float, rr: float = 2.0
) -> tuple[float, float]:
    """Compute (stop_loss, take_profit) given direction + ATR + R:R.

    Heuristic:
      raw_stop_pct = atr_h1 / entry
      stop_pct     = clamp(raw_stop_pct, MIN_STOP_PCT, MAX_STOP_PCT)
      stop_distance = entry * stop_pct
      sl = entry ± stop_distance      (losing side)
      tp = entry ± rr * stop_distance (winning side)

    Clamping the stop to a percentage of price prevents two real failure modes:
      - low-priced volatile coins (e.g. SOL with ATR ~$50 vs price ~$80) where
        a 1×ATR bracket produces nonsensical (negative) take profits;
      - quiet markets where ATR collapses near 0 and a literal 1×ATR stop
        sits inside the spread.
    """
    if entry <= 0:
        return 0.0, 0.0
    raw_pct = atr_h1 / entry if atr_h1 > 0 else MIN_STOP_PCT
    stop_pct = max(MIN_STOP_PCT, min(raw_pct, MAX_STOP_PCT))
    stop_distance = entry * stop_pct
    if direction == "long":
        sl = entry - stop_distance
        tp = entry + rr * stop_distance
    else:  # short
        sl = entry + stop_distance
        tp = entry - rr * stop_distance
    return sl, tp


def build_signal(
    *,
    ctx: MarketContext,
    report: ScoreReport,
    risk_gate: RiskGate,
    equity: float,
    atr_h1: float,
    mode: str,
    open_positions: int = 0,
    today_realized: float = 0.0,
    today_consecutive_losses: int = 0,
    in_blackout: bool = False,
    blackout_name: Optional[str] = None,
    rr: float = 2.0,
    now_ms: Optional[int] = None,
) -> Signal:
    """Assemble a Signal from a brain report + risk inputs.

    The returned Signal is ALWAYS valid (constructs without raising). Whether
    it's actionable — i.e. size > 0 and a real direction — depends on the
    report and the risk gates. Inspect `.is_actionable` before placing.

    The brain calls this once per evaluation; the executor (modes.py) decides
    whether to act on it based on mode + actionability."""
    now_ms = now_ms or int(time.time() * 1000)

    entry = ctx.current_price
    direction = report.direction
    if direction == "none" or atr_h1 <= 0:
        # No tradeable side — emit a record-only signal so the dashboard can
        # show 'why not'. SL/TP are nominal (entry +/- atr if known) so the
        # row is still well-formed.
        sl = entry - max(atr_h1, 1.0)
        tp = entry + max(atr_h1, 1.0)
    else:
        sl, tp = _stop_and_target(direction, entry, atr_h1, rr=rr)

    stop_distance = abs(entry - sl)
    decision = risk_gate.size_for(
        equity=equity,
        tier_multiplier=report.size_multiplier,
        stop_distance=stop_distance if direction in ("long", "short") else None,
        open_positions=open_positions,
        today_realized=today_realized,
        today_consecutive_losses=today_consecutive_losses,
        in_blackout=in_blackout,
        blackout_name=blackout_name,
    )

    criteria_payload = [
        {
            "name": r.name,
            "weight": r.weight,
            "passed": r.passed,
            "detail": r.detail,
            "direction_hint": r.direction_hint,
            "meta": r.meta,
        }
        for r in report.results
    ]

    return Signal(
        ts=now_ms,
        symbol=ctx.symbol,
        tier=report.tier,
        direction=direction,
        score=report.score,
        size_multiplier=report.size_multiplier,
        size=decision.size,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        mode=mode,
        criteria_json=json.dumps(criteria_payload),
        size_reason=decision.reason,
        notes=None,
    )


# ──────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────


async def persist_signal(signal: Signal) -> int:
    """Insert a Signal into the `signals` table, return its id."""
    async with session_factory()() as s:
        row = SignalRow(
            ts=signal.ts,
            symbol=signal.symbol,
            tier=signal.tier,
            direction=signal.direction,
            score=signal.score,
            size_pct=signal.size_multiplier,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            mode=signal.mode,
            criteria_json=signal.criteria_json,
            notes=signal.notes,
            executed=False,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id
