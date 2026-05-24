"""End-to-end smoke for the Telegram bot.

What it does (in order):
  1. Connects to Telegram with the configured token.
  2. Sends a /start-style hello to your chat.
  3. Sends a formatted demo signal (so you can see what alerts look like).
  4. Polls slash commands for 60 seconds — try /status, /help, /readiness,
     /pnl, /positions, /trip, /reset, /trips while it's running.
  5. Issues a request_approval to your chat (10s window). Tap Approve/Reject;
     the script prints the result.
  6. Stops cleanly.

The bot will keep responding to commands the whole time, then exit.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from cero.brain.risk import RiskGate
from cero.brain.signals import Signal
from cero.config import load_config
from cero.db.session import close_db, init_db
from cero.ui.telegram.bot import build_notifier


def demo_signal() -> Signal:
    return Signal(
        ts=0, symbol="ETH/USDT:USDT", tier="B", direction="long", score=72,
        size_multiplier=0.5, size=0.3125,
        entry_price=3000.00, stop_loss=2920.00, take_profit=3160.00,
        mode="approval", size_reason="ok (demo)",
    )


async def main() -> None:
    cfg, secrets = load_config()
    if not secrets.telegram_bot_token or not secrets.telegram_chat_id:
        print("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env — aborting")
        return

    # Throwaway DB so /trip and /reset write somewhere.
    tmp = Path(tempfile.gettempdir()) / "cero_smoke_tg.db"
    tmp.unlink(missing_ok=True)
    cfg.database.path = str(tmp)
    await init_db(cfg.database)

    risk_gate = RiskGate(cfg.risk, cfg.news)
    services = {"config": cfg, "risk_gate": risk_gate}

    notifier = build_notifier(
        secrets.telegram_bot_token,
        secrets.telegram_chat_id,
        services=services,
        backup_chat_id=secrets.telegram_chat_id_2,
    )
    if notifier is None:
        print("notifier creation failed — aborting")
        return

    await notifier.start()
    try:
        # 1) hello
        await notifier.send_notice("👋 cero telegram smoke is running")
        print("[1] sent hello")

        # 2) demo signal
        sig = demo_signal()
        await notifier.send_signal(sig)
        print("[2] sent demo signal")

        # 3) poll commands for 60 seconds so you can try slash commands
        print("[3] polling for 60s — try /status /help /readiness /positions /pnl /trips /trip /reset")
        await asyncio.sleep(60)

        # 4) approval request (10s window) — tap a button in Telegram
        print("[4] requesting approval (10s) — tap ✅ Approve or ❌ Reject in Telegram")
        result = await notifier.request_approval(sig, timeout_s=10.0)
        print(f"    approval result: {result}")

        await notifier.send_notice("smoke complete — stopping bot")
    finally:
        await notifier.stop()
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
    print("OK telegram smoke complete")


if __name__ == "__main__":
    asyncio.run(main())
