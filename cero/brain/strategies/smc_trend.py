"""SMC trend-following strategy — the original Cero strategy.

Wraps the existing 8-criteria scoring + signal-build pipeline as a
named Strategy. This is the strategy `docs/CRITERIA.md` documents.

Tier A/B requires `poi_alert` to pass (hard gate added Nov 2026 after
testnet validation showed 7.4% WR without it).
"""
from __future__ import annotations

from typing import Optional

from cero.brain.criteria import evaluate_all
from cero.brain.scoring import aggregate
from cero.brain.signals import Signal, build_signal
from cero.brain.strategies.base import Strategy, StrategyContext


class SmcTrendStrategy:
    """SMC trend-following: HTF trend + BOS confirmation + POI entry
    zone confluence. 8-criteria scoring with hard gate on poi_alert."""

    name: str = "smc_trend"

    async def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        results = evaluate_all(ctx.market)
        report = aggregate(results, ctx.risk_gate.risk)
        return build_signal(
            ctx=ctx.market,
            report=report,
            risk_gate=ctx.risk_gate,
            equity=ctx.equity,
            atr_h1=ctx.atr_h1,
            mode=ctx.mode,
            open_positions=ctx.open_positions,
            today_realized=ctx.today_realized,
            today_consecutive_losses=ctx.today_consecutive_losses,
            in_blackout=ctx.in_blackout,
            blackout_name=ctx.blackout_name,
            now_ms=ctx.market.now_ms,
            strategy=self.name,
        )
