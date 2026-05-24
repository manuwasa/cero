"""
Execution modes — signal_only, approval, auto.

Strategy pattern. Mode is selected from config; can be switched live via
Telegram command `/mode <name>`.

TODO (Claude Code):

class ExecutionMode(Protocol):
    async def handle_signal(self, signal: Signal) -> None: ...

class SignalOnlyMode:
    """Alert user; never place orders."""
    async def handle_signal(self, signal: Signal) -> None:
        await telegram.send_signal_alert(signal)

class ApprovalMode:
    """Ask user via inline button; place if approved within timeout."""
    async def handle_signal(self, signal: Signal) -> None:
        if signal.tier not in ("A", "B"):
            return
        approved = await telegram.request_approval(signal, timeout_s=60)
        if approved:
            await orders.place(signal)

class AutoMode:
    """Place orders within risk limits, no human in the loop."""
    async def handle_signal(self, signal: Signal) -> None:
        if state.tripped: return
        if signal.tier not in ("A", "B"): return
        await orders.place(signal)

def get_mode(name: str) -> ExecutionMode: ...
"""
from __future__ import annotations
