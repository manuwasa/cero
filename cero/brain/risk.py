"""
Risk management — position sizing, daily loss cap, consecutive-loss cap, TRIP.

The pure functions at the top of this file (`position_size`, `today_realized_pnl`,
`consecutive_losses`, `in_news_blackout`) take primitives and return decisions —
no DB, no exchange, fully unit-testable.

`RiskGate` ties them together with mutable state (the current trip status, the
open positions count) and persists trip events to the `trips` table. The brain
calls `gate.size_for(...)` to get a final position size; the executor calls
`gate.record_trade_close(...)` and `gate.trip(...)` as events happen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from loguru import logger
from sqlalchemy import select, update

from cero.config import NewsConfig, RiskConfig
from cero.db.models import CalendarEvent, TripEvent
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus

TripReason = Literal[
    "manual",
    "daily_loss",
    "consecutive_losses",
    "exchange_errors",
    "unexpected_position",
    "other",
]


# ──────────────────────────────────────────────────────────────────────
# Pure functions
# ──────────────────────────────────────────────────────────────────────


def position_size(
    *,
    equity: float,
    base_risk_pct: float,
    tier_multiplier: float,
    stop_distance: float,
) -> float:
    """How many contracts/coins to buy, sized so that hitting the stop loses
    exactly `base_risk_pct * tier_multiplier` percent of equity.

        risk_usd = equity * (base_risk_pct / 100) * tier_multiplier
        size     = risk_usd / stop_distance

    Inputs:
      equity          — current account equity, in quote currency (USDT)
      base_risk_pct   — risk per trade as a percent of equity (e.g. 0.5)
      tier_multiplier — 1.0 for tier A, 0.5 for B, 0.0 for C/D
      stop_distance   — |entry_price - stop_price|, in quote currency

    Returns 0.0 if any input is non-positive — never raises. The executor is
    responsible for rounding to the exchange's lot size."""
    if equity <= 0 or base_risk_pct <= 0 or tier_multiplier <= 0 or stop_distance <= 0:
        return 0.0
    risk_quote = equity * (base_risk_pct / 100.0) * tier_multiplier
    return risk_quote / stop_distance


def today_realized_pnl(trades: list, now_ms: Optional[int] = None) -> float:
    """Sum realized PnL of trades that closed on today's UTC date.
    `trades` is a list of objects with `closed_at: int` (unix-ms) and
    `realized_pnl: float`. Works with the SQLAlchemy `Trade` ORM row or any
    duck-typed object."""
    now_ms = now_ms or int(time.time() * 1000)
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    total = 0.0
    for t in trades:
        d = datetime.fromtimestamp(t.closed_at / 1000, tz=timezone.utc).date()
        if d == today:
            total += float(t.realized_pnl)
    return total


def consecutive_losses(trades: list) -> int:
    """Count losing trades from the end of the list backward, stopping at the
    first non-loss. `trades` must be sorted oldest → newest. A trade is a loss
    if `realized_pnl < 0`. A breakeven (== 0) breaks the streak."""
    count = 0
    for t in reversed(trades):
        if t.realized_pnl < 0:
            count += 1
        else:
            break
    return count


def in_news_blackout(
    events: list, now_ms: int, cfg: NewsConfig
) -> tuple[bool, Optional[str]]:
    """Return (blackout_active, event_name). True if `now_ms` falls within
    [event.ts - before, event.ts + after] for any event whose impact is in
    `cfg.blackout_impacts`."""
    before_ms = cfg.blackout_minutes_before * 60_000
    after_ms = cfg.blackout_minutes_after * 60_000
    for e in events:
        if e.impact not in cfg.blackout_impacts:
            continue
        if e.ts - before_ms <= now_ms <= e.ts + after_ms:
            return True, e.name
    return False, None


# ──────────────────────────────────────────────────────────────────────
# RiskGate — stateful façade
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SizingDecision:
    """The output of `RiskGate.size_for(...)`. Carries the why so the UI/log
    can show which gate stopped a trade rather than just "0 contracts"."""

    size: float
    reason: str            # human-readable explanation
    blocked_by: Optional[str] = None
    # 'tripped' | 'daily_loss' | 'consecutive_losses' | 'news_blackout'
    # | 'concurrent_positions' | 'no_stop' | 'tier' | None


class RiskGate:
    """Holds the live TRIP state and exposes the pre-trade gate.

    Workers and the executor share one instance via `cero/state.py` later.
    For now the brain instantiates it directly.

    Calls that mutate state (`trip`, `reset`, `record_trade_close`) hit the
    DB so dashboard reads stay consistent with in-memory state."""

    def __init__(
        self,
        risk_cfg: RiskConfig,
        news_cfg: NewsConfig,
        *,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.risk = risk_cfg
        self.news = news_cfg
        self.bus = event_bus or default_bus
        self._tripped: bool = False
        self._trip_reason: Optional[TripReason] = None
        self._trip_detail: str = ""
        self._log = logger.bind(component="risk")

    # ── state queries ─────────────────────────────────────────────────

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> Optional[TripReason]:
        return self._trip_reason

    @property
    def trip_detail(self) -> str:
        return self._trip_detail

    async def hydrate(self) -> None:
        """On boot, check the DB for an un-cleared trip and re-enter that state.
        Matches docs/ARCHITECTURE.md: 'Only un-trips via explicit /reset'."""
        async with session_factory()() as s:
            row = (
                await s.execute(
                    select(TripEvent)
                    .where(TripEvent.cleared_at.is_(None))
                    .order_by(TripEvent.fired_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is not None:
            self._tripped = True
            self._trip_reason = row.reason  # type: ignore[assignment]
            self._trip_detail = row.detail
            self._log.warning("hydrated existing TRIP: {} ({})", row.reason, row.detail)

    # ── pre-trade gate ────────────────────────────────────────────────

    def size_for(
        self,
        *,
        equity: float,
        tier_multiplier: float,
        stop_distance: Optional[float],
        open_positions: int,
        today_realized: float,
        today_consecutive_losses: int,
        in_blackout: bool,
        blackout_name: Optional[str] = None,
    ) -> SizingDecision:
        """Apply every gate in order; first failing gate wins. Returns a
        SizingDecision with `size=0` and `blocked_by=<gate>` if any gate fires."""
        if self._tripped:
            return SizingDecision(
                0.0, f"TRIPPED ({self._trip_reason}): {self._trip_detail}",
                blocked_by="tripped",
            )
        if tier_multiplier <= 0:
            return SizingDecision(0.0, "tier sizing is 0 (C or D)", blocked_by="tier")
        if stop_distance is None or stop_distance <= 0:
            return SizingDecision(0.0, "no stop distance provided", blocked_by="no_stop")
        if open_positions >= self.risk.max_concurrent_positions:
            return SizingDecision(
                0.0,
                f"max concurrent positions ({self.risk.max_concurrent_positions}) already open",
                blocked_by="concurrent_positions",
            )
        # Daily loss cap — compare against the configured percent of equity at
        # snapshot time. Loss is recorded as negative PnL.
        if today_realized < 0:
            loss_pct = abs(today_realized) / equity * 100.0 if equity > 0 else 0.0
            if loss_pct >= self.risk.max_daily_loss_pct:
                return SizingDecision(
                    0.0,
                    f"daily loss {loss_pct:.2f}% >= cap {self.risk.max_daily_loss_pct}%",
                    blocked_by="daily_loss",
                )
        if today_consecutive_losses >= self.risk.max_consecutive_losses:
            return SizingDecision(
                0.0,
                f"consecutive losses {today_consecutive_losses} >= "
                f"cap {self.risk.max_consecutive_losses}",
                blocked_by="consecutive_losses",
            )
        if in_blackout:
            return SizingDecision(
                0.0,
                f"news blackout: {blackout_name or 'high-impact event nearby'}",
                blocked_by="news_blackout",
            )

        size = position_size(
            equity=equity,
            base_risk_pct=self.risk.base_risk_per_trade_pct,
            tier_multiplier=tier_multiplier,
            stop_distance=stop_distance,
        )
        return SizingDecision(size, "ok")

    # ── mutations (persisted) ─────────────────────────────────────────

    async def trip(self, reason: TripReason, detail: str = "") -> int:
        """Mark the gate tripped and insert a TripEvent row. Idempotent — if
        already tripped, returns the existing row's id without inserting."""
        if self._tripped:
            self._log.warning("trip() called while already tripped — ignoring")
            return -1
        ts = int(time.time() * 1000)
        async with session_factory()() as s:
            row = TripEvent(fired_at=ts, reason=reason, detail=detail)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        self._tripped = True
        self._trip_reason = reason
        self._trip_detail = detail
        self._log.error("TRIPPED: {} — {}", reason, detail)
        # Notify any subscribers (e.g. TripWatcher cancels orders + closes
        # positions). DB write happens first so durability beats delivery.
        await self.bus.publish("trip:fired", {"reason": reason, "detail": detail})
        return row.id

    async def reset(self, by: str = "user") -> bool:
        """Clear the current trip (if any). Marks every un-cleared row as
        cleared so we don't leave stale rows behind. Returns True if a trip
        was actually cleared."""
        if not self._tripped:
            return False
        ts = int(time.time() * 1000)
        async with session_factory()() as s:
            await s.execute(
                update(TripEvent)
                .where(TripEvent.cleared_at.is_(None))
                .values(cleared_at=ts, cleared_by=by)
            )
            await s.commit()
        self._log.info("trip cleared by {}", by)
        self._tripped = False
        self._trip_reason = None
        self._trip_detail = ""
        return True

    def evaluate_trip_triggers(
        self,
        *,
        equity: float,
        today_realized: float,
        today_consecutive_losses: int,
    ) -> tuple[Optional[TripReason], str]:
        """Pure check — does the current state warrant a TRIP? Returns the
        reason + detail if so, else (None, "").

        Caller decides whether to actually call `await trip(...)`. Keeping
        this synchronous and pure makes it trivial to unit-test."""
        if self._tripped:
            return None, "already tripped"
        if equity > 0 and today_realized < 0:
            loss_pct = abs(today_realized) / equity * 100.0
            if loss_pct >= self.risk.max_daily_loss_pct:
                return "daily_loss", (
                    f"today's PnL {today_realized:.2f} = {loss_pct:.2f}% of equity "
                    f">= cap {self.risk.max_daily_loss_pct}%"
                )
        if today_consecutive_losses >= self.risk.max_consecutive_losses:
            return "consecutive_losses", (
                f"{today_consecutive_losses} consecutive losing trades "
                f">= cap {self.risk.max_consecutive_losses}"
            )
        return None, ""
