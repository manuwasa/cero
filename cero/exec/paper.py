"""
Paper trading broker.

A drop-in `OrderPlacer` that NEVER touches the exchange. It simulates the full
trade lifecycle against the live (mainnet) candle stream so you can run Cero
end-to-end — real prices, real brain, real risk gates — with zero money at risk.
This is the only safe way to find out whether a strategy makes money *before*
committing real funds (which, per this project's hard-won lesson, you do not do
until a strategy is validated).

Lifecycle:
  place(signal)        -> opens a simulated position at signal.entry_price,
                          records a Position row, tracks it in memory.
  (live candles)       -> the monitor loop watches each open position; when a
                          5m bar's high/low crosses SL or TP it closes the
                          position, writes a Trade row with realized PnL (net of
                          slippage+fees), and updates paper equity.
  after every close    -> re-checks the risk triggers (daily-loss %, consecutive
                          losses) and fires a real TRIP if breached — so the
                          loss-prevention gates are exercised exactly as they
                          would be live.

Costs match scripts/backtest_signals.py (0.1% slippage + 0.06% fee per leg) so
paper results line up with the offline backtest. Same-bar SL+TP is counted as a
loss (conservative), identical to the backtester.

Wire it in main.py for mode == 'paper'. It implements the same OrderPlacer
protocol as CcxtOrderPlacer, plus start()/stop() for the monitor task.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import desc, select

from cero.brain.risk import RiskGate
from cero.brain.signals import Signal
from cero.config import Config
from cero.data.exchange import Candle
from cero.db.models import Position, Signal as SignalRow, Trade
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus
from cero.exec.protocols import Notifier

# Match the realistic-cost backtest so paper PnL is comparable.
SLIPPAGE_PCT = 0.1    # per leg
FEE_PCT = 0.06        # per leg (bybit taker)


class _Open:
    """An open paper position, tracked in memory."""

    __slots__ = ("symbol", "side", "size", "entry", "sl", "tp", "opened_at", "signal_id")

    def __init__(self, symbol, side, size, entry, sl, tp, opened_at, signal_id):
        self.symbol = symbol
        self.side = side          # 'long' | 'short'
        self.size = size          # coins/contracts (positive)
        self.entry = entry
        self.sl = sl
        self.tp = tp
        self.opened_at = opened_at
        self.signal_id = signal_id


class PaperBroker:
    """Simulated OrderPlacer + position monitor. No exchange calls, ever."""

    name = "paper"

    def __init__(
        self,
        cfg: Config,
        risk_gate: RiskGate,
        notifier: Notifier,
        *,
        starting_equity: float = 10_000.0,
        trigger_tf: str = "5m",
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.cfg = cfg
        self.risk_gate = risk_gate
        self.notifier = notifier
        self.trigger_tf = trigger_tf
        self.bus = event_bus or default_bus
        self._equity = starting_equity
        self._start_equity = starting_equity
        self._day_realized = 0.0
        self._day = self._utc_day(int(time.time() * 1000))
        self._consec_losses = 0
        self._open: dict[str, _Open] = {}
        self._tasks: list[asyncio.Task] = []
        self._queues: list[tuple[str, asyncio.Queue]] = []
        self._stop = asyncio.Event()
        self._log = logger.bind(component="paper")

    # ── equity (fed to the brain for sizing) ───────────────────────────

    @property
    def equity(self) -> float:
        return self._equity

    # ── monitor lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to closed-bar events per symbol and watch open positions."""
        for symbol in self.cfg.symbols:
            topic = f"candle:closed:{symbol}:{self.trigger_tf}"
            q = self.bus.subscribe(topic)
            self._queues.append((topic, q))
            self._tasks.append(
                asyncio.create_task(self._watch(symbol, q), name=f"paper:{symbol}")
            )
        self._log.info(
            "paper broker started — equity={:.2f}, watching {} symbols",
            self._equity, len(self.cfg.symbols),
        )

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for topic, q in self._queues:
            self.bus.unsubscribe(topic, q)
        self._tasks.clear()
        self._queues.clear()

    async def _watch(self, symbol: str, q: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                candle = await q.get()
                await self.check_exits(candle)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._log.exception("paper monitor {} crashed: {}", symbol, e)

    # ── OrderPlacer protocol ───────────────────────────────────────────

    async def place(self, signal: Signal) -> Optional[str]:
        if not signal.is_actionable:
            return None
        if signal.symbol in self._open:
            self._log.info("[{}] paper position already open — skip", signal.symbol)
            return None

        signal_id = await self._lookup_signal_id(signal)
        pos = _Open(
            symbol=signal.symbol, side=signal.direction, size=signal.size,
            entry=signal.entry_price, sl=signal.stop_loss, tp=signal.take_profit,
            opened_at=signal.ts, signal_id=signal_id,
        )
        self._open[signal.symbol] = pos
        await self._record_open(pos, signal_id)
        self._log.info(
            "PAPER OPEN {} {} size={:.6f} entry={:.4f} sl={:.4f} tp={:.4f}",
            pos.symbol, pos.side, pos.size, pos.entry, pos.sl, pos.tp,
        )
        await self.notifier.send_notice(
            f"[PAPER] open {pos.side} {pos.symbol} @ {pos.entry:.4f} "
            f"(sl {pos.sl:.4f} / tp {pos.tp:.4f})"
        )
        return f"paper-{pos.opened_at}"

    async def cancel_all_for(self, symbol: str) -> None:
        # No resting orders in paper; nothing to cancel.
        return None

    async def close_position(self, symbol: str, *, price: Optional[float] = None) -> None:
        """Close an open paper position at `price` (or its TP/SL midpoint if
        unknown). Used by the TripWatcher on a TRIP."""
        pos = self._open.get(symbol)
        if pos is None:
            return
        exit_price = price if price is not None else pos.entry
        await self._close(pos, exit_price, "trip")

    # ── exit simulation ────────────────────────────────────────────────

    async def check_exits(self, candle: Candle) -> None:
        """Given a freshly-closed bar, close the matching open position if its
        high/low crossed SL or TP. SL is checked first (same-bar = loss),
        identical to the backtester."""
        pos = self._open.get(candle.symbol)
        if pos is None:
            return
        if pos.side == "long":
            if candle.low <= pos.sl:
                await self._close(pos, pos.sl, "sl")
            elif candle.high >= pos.tp:
                await self._close(pos, pos.tp, "tp")
        else:  # short
            if candle.high >= pos.sl:
                await self._close(pos, pos.sl, "sl")
            elif candle.low <= pos.tp:
                await self._close(pos, pos.tp, "tp")

    async def _close(self, pos: _Open, exit_price: float, reason: str) -> None:
        self._open.pop(pos.symbol, None)
        # Gross PnL in quote currency.
        if pos.side == "long":
            gross = pos.size * (exit_price - pos.entry)
        else:
            gross = pos.size * (pos.entry - exit_price)
        # Costs: slippage + fee on both legs, as a fraction of notional.
        notional = pos.size * pos.entry
        costs = notional * (SLIPPAGE_PCT + FEE_PCT) / 100 * 2
        pnl = gross - costs

        now_ms = int(time.time() * 1000)
        self._equity += pnl
        self._roll_day(now_ms)
        self._day_realized += pnl
        if pnl < 0:
            self._consec_losses += 1
        else:
            self._consec_losses = 0

        await self._record_close(pos, exit_price, pnl, costs, reason, now_ms)
        self._log.info(
            "PAPER CLOSE {} {} @ {:.4f} ({}) pnl={:+.2f} equity={:.2f} consec_losses={}",
            pos.symbol, pos.side, exit_price, reason, pnl, self._equity, self._consec_losses,
        )
        await self.notifier.send_notice(
            f"[PAPER] close {pos.symbol} ({reason}) pnl={pnl:+.2f} → equity {self._equity:.2f}"
        )
        await self.bus.publish("paper:close", {"symbol": pos.symbol, "pnl": pnl})
        await self._maybe_trip(now_ms)

    # ── loss-prevention: fire real TRIPs on breach ─────────────────────

    async def _maybe_trip(self, now_ms: int) -> None:
        """Exercise the same risk triggers the live system would. If the daily
        loss cap or consecutive-loss cap is breached, fire a real TRIP — which
        halts new trades (size_for returns 0) and tells the TripWatcher to flat
        everything. This is the loss-prevention the user asked for."""
        if self.risk_gate.tripped:
            return
        reason, detail = self.risk_gate.evaluate_trip_triggers(
            equity=self._equity,
            today_realized=self._day_realized,
            today_consecutive_losses=self._consec_losses,
        )
        if reason is not None:
            self._log.warning("PAPER TRIP: {} — {}", reason, detail)
            await self.risk_gate.trip(reason, f"[paper] {detail}")

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _utc_day(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()

    def _roll_day(self, now_ms: int) -> None:
        today = self._utc_day(now_ms)
        if today != self._day:
            self._day = today
            self._day_realized = 0.0   # daily loss cap resets at UTC midnight

    async def _lookup_signal_id(self, signal: Signal) -> Optional[int]:
        async with session_factory()() as s:
            row = (
                await s.execute(
                    select(SignalRow)
                    .where(SignalRow.symbol == signal.symbol)
                    .where(SignalRow.ts == signal.ts)
                    .order_by(desc(SignalRow.id)).limit(1)
                )
            ).scalar_one_or_none()
            return row.id if row else None

    async def _record_open(self, pos: _Open, signal_id: Optional[int]) -> None:
        size_signed = pos.size if pos.side == "long" else -pos.size
        async with session_factory()() as s:
            s.add(Position(
                exchange_position_id=f"paper-{pos.symbol}-{pos.opened_at}",
                symbol=pos.symbol, side=pos.side, size=size_signed,
                entry_price=pos.entry, mark_price=pos.entry,
                leverage=float(self.cfg.exchange.leverage),
                stop_loss=pos.sl, take_profit=pos.tp,
                opened_at=pos.opened_at, updated_at=pos.opened_at,
                signal_id=signal_id,
            ))
            await s.commit()

    async def _record_close(
        self, pos: _Open, exit_price: float, pnl: float, fees: float,
        reason: str, now_ms: int,
    ) -> None:
        size_signed = pos.size if pos.side == "long" else -pos.size
        async with session_factory()() as s:
            s.add(Trade(
                symbol=pos.symbol, side=pos.side, size=size_signed,
                entry_price=pos.entry, exit_price=exit_price,
                opened_at=pos.opened_at, closed_at=now_ms,
                realized_pnl=pnl, fees=fees, exit_reason=reason,
                signal_id=pos.signal_id,
            ))
            # Remove the open Position row (matched by our paper id).
            rows = (await s.execute(
                select(Position).where(
                    Position.exchange_position_id == f"paper-{pos.symbol}-{pos.opened_at}"
                )
            )).scalars().all()
            for r in rows:
                await s.delete(r)
            await s.commit()
