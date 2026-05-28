"""
Execution modes — strategy pattern over Signal handling.

Three modes, one method (`handle_signal`):
  - **signal_only**: just notify the user; never place an order.
  - **approval**:    ask via the notifier; place if approved within timeout.
  - **auto**:        place immediately if A/B-tier and not tripped.

Mode is chosen from `config.mode` at boot; can be hot-swapped via `set_mode()`
when the Telegram bot lands.

Also defined here:
  - `LogNotifier` — stand-in Notifier that prints via loguru; used until
    `cero/ui/telegram/bot.py` lands in step 9.
  - `StubOrderPlacer` — records the placement call but doesn't touch the
    exchange. The real OrderPlacer (step 10) goes in `cero/exec/orders.py`.
  - `TripWatcher` — subscribes to `bus("trip:fired")`; on fire it cancels
    every open order and closes every open position via the OrderPlacer.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Protocol

from loguru import logger
from sqlalchemy import update

from cero.brain.risk import RiskGate
from cero.brain.signals import Signal
from cero.db.models import Signal as SignalRow
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus
from cero.exec.protocols import Notifier, OrderPlacer


Mode = str   # 'signal_only' | 'approval' | 'auto'

TRIP_TOPIC = "trip:fired"
SIGNAL_TOPIC = "signal:new"


# ──────────────────────────────────────────────────────────────────────
# ExecutionMode interface
# ──────────────────────────────────────────────────────────────────────


class ExecutionMode(Protocol):
    name: str
    async def handle_signal(self, signal: Signal) -> None: ...


async def _mark_executed(signal_id: int) -> None:
    """Flip `signals.executed = True` for a row id."""
    async with session_factory()() as s:
        await s.execute(update(SignalRow).where(SignalRow.id == signal_id).values(executed=True))
        await s.commit()


# ──────────────────────────────────────────────────────────────────────
# Concrete modes
# ──────────────────────────────────────────────────────────────────────


class SignalOnlyMode:
    """Alert the user; never place orders. The starting mode while learning."""

    name = "signal_only"

    def __init__(self, notifier: Notifier) -> None:
        self.notifier = notifier
        self._log = logger.bind(mode=self.name)

    async def handle_signal(self, signal: Signal) -> None:
        if not signal.is_actionable:
            self._log.info(
                "[{}] tier={} dir={} score={} — non-actionable, skipping notify",
                signal.symbol, signal.tier, signal.direction, signal.score,
            )
            return
        await self.notifier.send_signal(signal)
        self._log.info(
            "[{}] tier={} dir={} score={} size={:.6f} entry={:.2f} sl={:.2f} tp={:.2f}",
            signal.symbol, signal.tier, signal.direction, signal.score,
            signal.size, signal.entry_price, signal.stop_loss, signal.take_profit,
        )


class ApprovalMode:
    """Ask the user; place only on explicit approval within the timeout window."""

    name = "approval"

    def __init__(
        self,
        notifier: Notifier,
        placer: OrderPlacer,
        risk_gate: RiskGate,
        *,
        timeout_s: float = 60.0,
    ) -> None:
        self.notifier = notifier
        self.placer = placer
        self.risk_gate = risk_gate
        self.timeout_s = timeout_s
        self._log = logger.bind(mode=self.name)

    async def handle_signal(self, signal: Signal) -> None:
        if not signal.is_actionable:
            await self.notifier.send_signal(signal)
            self._log.info("[{}] not actionable ({}); notify only",
                           signal.symbol, signal.size_reason)
            return
        if self.risk_gate.tripped:
            await self.notifier.send_notice(
                f"signal suppressed: TRIPPED ({self.risk_gate.trip_reason})"
            )
            return

        approved = await self.notifier.request_approval(signal, self.timeout_s)
        if not approved:
            self._log.info("[{}] approval declined / timed out", signal.symbol)
            return
        await self.placer.place(signal)


class AutoMode:
    """Place orders within risk limits. No human in the loop, but still gated
    by tier (A/B only) and trip state."""

    name = "auto"

    def __init__(
        self,
        notifier: Notifier,
        placer: OrderPlacer,
        risk_gate: RiskGate,
    ) -> None:
        self.notifier = notifier
        self.placer = placer
        self.risk_gate = risk_gate
        self._log = logger.bind(mode=self.name)

    async def handle_signal(self, signal: Signal) -> None:
        if self.risk_gate.tripped:
            self._log.warning("[{}] suppressed: TRIPPED", signal.symbol)
            return
        if signal.tier not in ("A", "B"):
            await self.notifier.send_signal(signal)  # still surface for visibility
            self._log.info("[{}] not actionable: tier={}", signal.symbol, signal.tier)
            return
        if not signal.is_actionable:
            await self.notifier.send_signal(signal)
            self._log.info("[{}] not actionable: {}", signal.symbol, signal.size_reason)
            return
        await self.notifier.send_signal(signal)
        await self.placer.place(signal)


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────


def build_mode(
    name: Mode,
    *,
    notifier: Notifier,
    placer: OrderPlacer,
    risk_gate: RiskGate,
    approval_timeout_s: float = 60.0,
) -> ExecutionMode:
    """Pick a mode by name. Raises ValueError on unknown names so a typo in
    config.yaml fails loudly at boot."""
    if name == "signal_only":
        return SignalOnlyMode(notifier=notifier)
    if name == "approval":
        return ApprovalMode(
            notifier=notifier, placer=placer, risk_gate=risk_gate,
            timeout_s=approval_timeout_s,
        )
    if name == "auto":
        return AutoMode(notifier=notifier, placer=placer, risk_gate=risk_gate)
    raise ValueError(f"unknown execution mode: {name!r}")


# ──────────────────────────────────────────────────────────────────────
# TripWatcher
# ──────────────────────────────────────────────────────────────────────


class TripWatcher:
    """Subscribes to `bus(TRIP_TOPIC)`; when a trip event arrives:
      1. Cancel every open order for every configured symbol.
      2. Close every open position at market.
      3. Send a Notifier 'TRIPPED' notice.

    The brain's `RiskGate.trip()` publishes to TRIP_TOPIC after persisting.
    This separation keeps risk decision-making in the brain and side effects
    in exec, matching docs/ARCHITECTURE.md.
    """

    def __init__(
        self,
        notifier: Notifier,
        placer: OrderPlacer,
        symbols: list[str],
        *,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.notifier = notifier
        self.placer = placer
        self.symbols = symbols
        self.bus = event_bus or default_bus
        self._task: Optional[asyncio.Task[None]] = None
        self._log = logger.bind(component="trip_watcher")

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("TripWatcher already started")
        q = self.bus.subscribe(TRIP_TOPIC)
        self._task = asyncio.create_task(self._loop(q), name="trip_watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    async def _loop(self, q: asyncio.Queue) -> None:
        while True:
            msg = await q.get()
            try:
                await self._handle(msg)
            except Exception as e:  # noqa: BLE001 — trip handler must never crash the system
                self._log.exception("trip handler crashed: {}", e)

    async def _handle(self, msg: dict) -> None:
        reason = msg.get("reason", "unknown") if isinstance(msg, dict) else "unknown"
        detail = msg.get("detail", "") if isinstance(msg, dict) else ""
        self._log.error("TRIP received ({}): {}", reason, detail)
        await self.notifier.send_notice(f"TRIPPED ({reason}): {detail}")
        for sym in self.symbols:
            try:
                await self.placer.cancel_all_for(sym)
            except Exception as e:  # noqa: BLE001
                self._log.exception("cancel_all_for({}) failed: {}", sym, e)
            try:
                await self.placer.close_position(sym)
            except Exception as e:  # noqa: BLE001
                self._log.exception("close_position({}) failed: {}", sym, e)


# ──────────────────────────────────────────────────────────────────────
# Built-in stand-in implementations
# ──────────────────────────────────────────────────────────────────────


class LogNotifier:
    """Notifier that just logs via loguru. Used until the Telegram bot is wired.
    `request_approval` always returns False so approval mode is safe-by-default
    when this notifier is the only one available."""

    def __init__(self) -> None:
        self._log = logger.bind(component="notifier")

    async def send_signal(self, signal: Signal) -> None:
        self._log.info(
            "SIGNAL  {} tier={} dir={} score={} size={:.6f} entry={:.2f} sl={:.2f} tp={:.2f}  ({})",
            signal.symbol, signal.tier, signal.direction, signal.score,
            signal.size, signal.entry_price, signal.stop_loss, signal.take_profit,
            signal.size_reason,
        )

    async def send_notice(self, text: str) -> None:
        self._log.info("NOTICE  {}", text)

    async def request_approval(self, signal: Signal, timeout_s: float) -> bool:
        self._log.warning(
            "approval requested via LogNotifier — no UI to ask, returning False"
        )
        return False


class StubOrderPlacer:
    """OrderPlacer that records the call but doesn't touch the exchange.
    Replaced by `cero/exec/orders.py` in step 10. Useful for tests and for
    running modes end-to-end in signal_only without risking testnet state."""

    def __init__(self) -> None:
        self.placed: list[Signal] = []
        self.canceled: list[str] = []
        self.closed: list[str] = []
        self._log = logger.bind(component="placer:stub")

    async def place(self, signal: Signal) -> Optional[str]:
        self.placed.append(signal)
        order_id = f"stub-{len(self.placed):04d}"
        self._log.info("PLACE  {} (stub order_id={})", signal.symbol, order_id)
        return order_id

    async def cancel_all_for(self, symbol: str) -> None:
        self.canceled.append(symbol)
        self._log.info("CANCEL {}", symbol)

    async def close_position(self, symbol: str) -> None:
        self.closed.append(symbol)
        self._log.info("CLOSE  {}", symbol)
