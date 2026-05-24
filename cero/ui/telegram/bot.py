"""
Telegram bot setup.

Uses aiogram. Starts as an asyncio task; lives for the process lifetime.

TODO (Claude Code):

async def run(config: Config) -> None:
    bot = Bot(token=secrets.telegram_bot_token)
    dp = Dispatcher()
    register_handlers(dp)
    await dp.start_polling(bot)

Helper functions used by other modules:
    async def send(text: str) -> None
    async def send_signal_alert(signal: Signal) -> None
    async def request_approval(signal: Signal, timeout_s: int) -> bool
    async def send_error(error: Exception) -> None
"""
from __future__ import annotations
