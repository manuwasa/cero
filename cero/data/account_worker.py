"""
Account worker — keeps balance + positions in sync with the exchange.

Every `cfg.account_poll_seconds` it:
  1. fetch_balance → writes a new AccountSnapshot row.
  2. fetch_positions → reconciles against the `positions` table:
       - **new** position the exchange shows but we don't track → either an
         orphan from a prior process (first tick: imported quietly) or a
         manual trade placed elsewhere (later ticks: TRIP).
       - **changed** position → update mark_price / uPnL / SL / TP.
       - **closed** position (we tracked it but exchange no longer shows
         it) → delete the Position row. Trade row creation lives in
         the order/fill watcher (future step) so we don't fabricate fills.

The "first tick imports, subsequent ticks TRIP" heuristic catches the common
real-world case where Cero restarts and finds existing positions on the
exchange — without that grace, every restart would trip.

Supervised the same way as price_worker: a crash backs off and the worker
restarts. A bad poll never takes down the rest of the process.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger
from sqlalchemy import delete, select, update

from cero.brain.risk import RiskGate
from cero.config import Config
from cero.data.exchange import ExchangeClient, PositionInfo
from cero.db.models import AccountSnapshot, Position as PositionRow, Trade as TradeRow
from cero.db.session import session_factory


class AccountWorker:
    """Periodic REST poll of balance + positions."""

    def __init__(
        self,
        cfg: Config,
        exchange: ExchangeClient,
        risk_gate: RiskGate,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.risk_gate = risk_gate
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._initialized = False
        # Track exchange position ids we've seen across polls. New ids that
        # weren't in last_seen and appear on a non-first tick are flagged
        # unexpected.
        self._known_ids: set[str] = set()
        self._log = logger.bind(worker="account")

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("AccountWorker already started")
        self._task = asyncio.create_task(self._loop(), name="account_worker")
        self._log.info(
            "started (interval={}s)", self.cfg.account_poll_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._log.info("stopped")

    # ── main loop ─────────────────────────────────────────────────────

    async def _loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._tick()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(60.0, 2.0**attempt)
                self._log.exception(
                    "tick crashed (attempt {}): {} — sleeping {}s",
                    attempt, e, delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue
            # Normal pacing.
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.account_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    # ── tick ──────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        await self._snapshot_balance()
        await self._reconcile_positions()
        self._initialized = True

    async def _snapshot_balance(self) -> None:
        bal = await self.exchange.fetch_balance()
        async with session_factory()() as s:
            s.add(AccountSnapshot(
                ts=int(time.time() * 1000),
                equity=bal.equity, balance=bal.balance,
                unrealized_pnl=bal.unrealized_pnl, margin_used=bal.margin_used,
                quote_currency=bal.quote_currency,
            ))
            await s.commit()

    async def _reconcile_positions(self) -> None:
        live = await self.exchange.fetch_positions()
        live_by_id = {p.exchange_position_id or _key(p): p for p in live}
        live_ids = set(live_by_id.keys())

        # Snapshot existing rows so we can diff.
        async with session_factory()() as s:
            rows = (
                await s.execute(select(PositionRow))
            ).scalars().all()
            tracked_ids = {
                (r.exchange_position_id or f"{r.symbol}:{r.side}") for r in rows
            }
            tracked_by_id = {
                (r.exchange_position_id or f"{r.symbol}:{r.side}"): r for r in rows
            }

        new_ids = live_ids - tracked_ids
        gone_ids = tracked_ids - live_ids

        # Unexpected-position detection runs only on non-first ticks. On the
        # first tick we silently import everything we find — that's the post-
        # restart reconciliation path.
        unexpected = (new_ids - self._known_ids) if self._initialized else set()

        if unexpected:
            sample = next(iter(unexpected))
            p = live_by_id[sample]
            detail = (
                f"unexpected position {p.symbol} {p.side} size={p.size} "
                f"(exch_id={p.exchange_position_id})"
            )
            self._log.error("UNEXPECTED POSITION → TRIP: {}", detail)
            await self.risk_gate.trip("unexpected_position", detail)
            # Even though we tripped, still record what we saw so the dashboard
            # reflects truth. The TripWatcher will close everything separately.

        # Apply additions + updates + deletions in a single tx.
        async with session_factory()() as s:
            # additions
            for pid in new_ids:
                p = live_by_id[pid]
                s.add(_row_from_info(p))
            # updates (mark/uPnL/SL/TP changes)
            for pid in live_ids & tracked_ids:
                p = live_by_id[pid]
                _update_row(tracked_by_id[pid], p)
                # SQLAlchemy 2.0 unit-of-work: in-place mutation tracked.
                s.add(tracked_by_id[pid])
            # deletions — a position the exchange no longer reports has closed.
            # Write a Trade row capturing what we know before deleting the
            # Position so PnL stats include it. We don't know the exact exit
            # price (fills could have been at SL, TP, or anywhere in between),
            # so we approximate using the last `mark_price` we observed. For
            # tight validation later, swap this for a fetch_closed_orders call.
            if gone_ids:
                for pid in gone_ids:
                    row = tracked_by_id[pid]
                    s.add(_trade_row_from_closed_position(row))
                    await s.execute(
                        delete(PositionRow).where(PositionRow.id == row.id)
                    )
            await s.commit()

        self._known_ids = live_ids


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────


def _key(p: PositionInfo) -> str:
    """Fallback key when the exchange doesn't return a position id."""
    return f"{p.symbol}:{p.side}"


def _row_from_info(p: PositionInfo) -> PositionRow:
    now = int(time.time() * 1000)
    return PositionRow(
        exchange_position_id=p.exchange_position_id,
        symbol=p.symbol,
        side=p.side,
        size=p.size,
        entry_price=p.entry_price,
        mark_price=p.mark_price,
        leverage=p.leverage,
        stop_loss=p.stop_loss,
        take_profit=p.take_profit,
        unrealized_pnl=p.unrealized_pnl,
        opened_at=now,
        updated_at=now,
    )


def _update_row(row: PositionRow, p: PositionInfo) -> None:
    row.mark_price = p.mark_price
    row.unrealized_pnl = p.unrealized_pnl
    row.stop_loss = p.stop_loss
    row.take_profit = p.take_profit
    row.size = p.size
    row.updated_at = int(time.time() * 1000)


def _trade_row_from_closed_position(row: PositionRow) -> TradeRow:
    """Build a TradeRow from a Position that's about to be deleted.

    Approximations:
      - exit_price: last observed mark_price (the actual fill was likely at
        SL or TP, but we don't have fetch_closed_orders wired yet).
      - realized_pnl: computed from (exit - entry) * signed_size, also an
        approximation. Sign is correct, magnitude is roughly right when the
        position closed near the mark we last saw.
      - exit_reason: 'other' since we can't distinguish SL/TP/manual without
        the closed-orders endpoint. A future fill-watcher would set this
        precisely.

    These approximations are good enough for validation-gate counting
    (trade count, win rate sign) but NOT for cent-accurate accounting. For
    real PnL, cross-reference with the exchange's closed-PnL report."""
    size_abs = abs(row.size)
    entry = row.entry_price
    exit_p = row.mark_price
    if row.side == "long":
        realized = (exit_p - entry) * size_abs
    else:  # short
        realized = (entry - exit_p) * size_abs
    return TradeRow(
        symbol=row.symbol,
        side=row.side,
        size=size_abs,
        entry_price=entry,
        exit_price=exit_p,
        opened_at=row.opened_at,
        closed_at=int(time.time() * 1000),
        realized_pnl=realized,
        fees=0.0,                  # we don't track fees yet
        exit_reason="other",
        signal_id=row.signal_id,
    )
