"""Strategy abstraction.

A Strategy is a function (MarketContext + risk inputs) → optional Signal.

Multiple strategies can be registered and run on every brain tick. The
scheduler persists all returned signals tagged with the strategy name.
Only signals from `cfg.primary_strategy` reach the executor — the rest
accumulate as **shadow data** for A/B comparison.

This is the seam that lets us iterate on alternative strategies without
risking the live one. Run two strategies for a week, see which performs
better, swap `primary_strategy` if appropriate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from cero.brain.criteria import MarketContext
from cero.brain.risk import RiskGate
from cero.brain.signals import Signal


@dataclass(frozen=True)
class StrategyContext:
    """All inputs a strategy needs to evaluate one (symbol, tick).
    Built once per tick by the scheduler and passed to every strategy."""

    market: MarketContext
    risk_gate: RiskGate
    equity: float
    atr_h1: float
    mode: str
    open_positions: int
    today_realized: float
    today_consecutive_losses: int
    in_blackout: bool
    blackout_name: Optional[str]


class Strategy(Protocol):
    """A strategy is anything that can name itself and produce a Signal
    (or None) given a StrategyContext. Implementations live in
    cero/brain/strategies/<name>.py.

    Returning None means "no signal for this strategy on this tick".
    Strategies should NOT do I/O — they read from the context and return
    a Signal. The scheduler handles persistence + dispatch."""

    name: str

    async def evaluate(self, ctx: StrategyContext) -> Optional[Signal]: ...
