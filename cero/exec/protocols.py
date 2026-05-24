"""
Abstractions the execution modes depend on.

Defining these as Protocols (not ABCs) lets us swap implementations freely:
  - LogNotifier (built-in) → TelegramNotifier (step 9)
  - StubOrderPlacer (built-in) → CcxtOrderPlacer (step 10)

Tests pass mocks that satisfy the same shape. No real I/O happens at this
layer — concrete implementations live in their own modules.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from cero.brain.signals import Signal


@runtime_checkable
class Notifier(Protocol):
    """Anything that can push messages to the user (Telegram, log, etc.)."""

    async def send_signal(self, signal: Signal) -> None:
        """Fire-and-forget alert: 'new tier-X signal on SYMBOL'."""

    async def send_notice(self, text: str) -> None:
        """Plain text alert (trip fired, executor error, etc.)."""

    async def request_approval(self, signal: Signal, timeout_s: float) -> bool:
        """Ask the user to approve `signal`. Return True on approval, False on
        rejection OR timeout. Must never raise on timeout."""


@runtime_checkable
class OrderPlacer(Protocol):
    """Anything that can place / cancel / close orders on the exchange."""

    async def place(self, signal: Signal) -> Optional[str]:
        """Submit the entry order plus SL/TP brackets. Return the exchange
        order id (or None if the placement was a no-op / failed cleanly).
        Implementations are responsible for recording the trade in the DB."""

    async def cancel_all_for(self, symbol: str) -> None:
        """Cancel every open order for `symbol`. Used by the TRIP handler."""

    async def close_position(self, symbol: str) -> None:
        """Close any open position on `symbol` at market. Used by TRIP."""
