"""
Signal emission.

The brain's outer layer. Combines scoring + direction + risk into a Signal
event. Writes to `signals` table; publishes "signal:new" event for the executor.

A Signal is ONLY emitted when state changes meaningfully:
    - tier improves to A or B
    - direction flips
    - tier drops below trade threshold (cancel pending)

TODO (Claude Code):

@dataclass(frozen=True)
class Signal:
    id: str
    symbol: str
    timestamp: datetime
    tier: Literal["A", "B", "C", "D"]
    score: int
    direction: Literal["long", "short", "none"]
    entry: float
    stop_loss: float
    take_profit: float
    size: float
    reason: str

async def evaluate_symbol(symbol: str) -> Signal | None: ...
async def scheduler() -> None:
    # On each candle tick, evaluate all configured symbols
    ...
"""
from __future__ import annotations
