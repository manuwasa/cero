"""Strategy registry — multiple strategies run in parallel, scheduler
hands signals from `primary_strategy` to the executor and persists all
the rest as shadow data for comparison."""
from __future__ import annotations

from cero.brain.strategies.base import Strategy, StrategyContext
from cero.brain.strategies.mean_reversion import MeanReversionStrategy
from cero.brain.strategies.smc_trend import SmcTrendStrategy

__all__ = [
    "Strategy",
    "StrategyContext",
    "SmcTrendStrategy",
    "MeanReversionStrategy",
    "ALL_STRATEGIES",
]

# Live strategy registry — only these run each tick and persist signals.
# mean_reversion is intentionally NOT registered: its implementation is broken
# (actual RR 0.5–16.9 vs the intended ~1.5, plus duplicate signals) and its
# premise did not convert to profit on clean mainnet data. It stays importable
# for reference / future rework, but no longer runs as shadow data — it was just
# cluttering morning_check with losing signals that never trade. Re-add it here
# to resume shadow comparison once it's fixed.
ALL_STRATEGIES: list[Strategy] = [
    SmcTrendStrategy(),
]
