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

# Default strategy registry. Order matters for output stability (alphabetical).
ALL_STRATEGIES: list[Strategy] = [
    MeanReversionStrategy(),
    SmcTrendStrategy(),
]
