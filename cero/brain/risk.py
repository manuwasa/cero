"""
Risk management — sizing, daily loss caps, TRIP logic.

PURE FUNCTIONS where possible. Reads account state + config; returns decisions.

TODO (Claude Code):

def position_size(
    equity: float,
    base_risk_pct: float,
    tier: str,
    tier_sizing: dict[str, float],
    stop_distance: float,
) -> float:
    risk_usd = equity * (base_risk_pct / 100) * tier_sizing[tier]
    qty = risk_usd / stop_distance
    return round_to_lot(qty)

def should_trip(account: AccountSnapshot, todays_trades: list[Trade], config: RiskConfig) -> tuple[bool, str]:
    # Returns (should_trip, reason)
    if daily_loss_pct(account, todays_trades) > config.max_daily_loss_pct:
        return True, "daily loss exceeded"
    if consecutive_losses(todays_trades) >= config.max_consecutive_losses:
        return True, "consecutive losses exceeded"
    return False, ""

def in_news_blackout(events: list[Event], now: datetime, config: NewsConfig) -> bool: ...
"""
from __future__ import annotations
