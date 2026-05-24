"""
Telegram bot — implements the `Notifier` protocol and runs a polling
dispatcher for slash commands.

Owns the bridge between async approval requests (modes.ApprovalMode) and
button callbacks: when `request_approval(signal, timeout_s)` is called, it
sends an inline keyboard, stores an `asyncio.Future` keyed by a short id, and
the callback handler resolves it. Times out cleanly.

Lifecycle:
    notifier = TelegramNotifier(token, chat_id, services=...)
    await notifier.start()      # spawns the polling task
    await notifier.send_signal(signal)
    await notifier.stop()
"""
from __future__ import annotations

import asyncio
import secrets as _secrets
import time
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from cero.brain.signals import Signal

# ──────────────────────────────────────────────────────────────────────
# Formatting helpers (pure)
# ──────────────────────────────────────────────────────────────────────


_TIER_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}
_DIR_EMOJI = {"long": "📈", "short": "📉", "none": "⏸"}


def format_signal(signal: Signal) -> str:
    """Render a Signal as a Telegram-friendly message (HTML parse mode)."""
    tier_e = _TIER_EMOJI.get(signal.tier, "❓")
    dir_e = _DIR_EMOJI.get(signal.direction, "❓")
    actionable = "✅ actionable" if signal.is_actionable else "⏸ informational"
    return (
        f"{tier_e} <b>{signal.symbol}</b>  {dir_e} <b>{signal.direction}</b>\n"
        f"<b>Tier {signal.tier}</b>  •  score <b>{signal.score}/100</b>  •  {actionable}\n"
        f"\n"
        f"<code>entry  {signal.entry_price:>10.2f}</code>\n"
        f"<code>stop   {signal.stop_loss:>10.2f}</code>\n"
        f"<code>target {signal.take_profit:>10.2f}</code>\n"
        f"<code>size   {signal.size:>10.6f}</code>  (×{signal.size_multiplier})\n"
        f"\n"
        f"<i>{_escape(signal.size_reason)}</i>"
    )


def _escape(text: str) -> str:
    """Minimal HTML escape for Telegram parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{approval_id}"),
        ]]
    )


# ──────────────────────────────────────────────────────────────────────
# TelegramNotifier
# ──────────────────────────────────────────────────────────────────────


class TelegramNotifier:
    """Implements `cero.exec.protocols.Notifier`. Also runs the slash-command
    dispatcher in a background task.

    `services` is a dict of accessors handlers use to read state without
    importing the whole world. See `cero/ui/telegram/handlers.py` for the
    expected keys (`risk_gate`, `mode_provider`, etc.). Optional —
    handlers gracefully degrade when a service isn't wired."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        services: Optional[dict] = None,
        allowed_chat_ids: Optional[list[str]] = None,
    ) -> None:
        if not token:
            raise ValueError("TelegramNotifier: empty token")
        if not chat_id:
            raise ValueError("TelegramNotifier: empty chat_id")

        self.token = token
        self.chat_id = str(chat_id)
        self.allowed_chat_ids = set(str(c) for c in (allowed_chat_ids or [chat_id]))
        self.services = services or {}

        # aiogram defaults aiohttp to aiodns, which can't find DNS servers on
        # some Windows setups (same root cause as the ccxt fix in
        # cero/data/exchange.py). We swap in a ThreadedResolver in start(),
        # because aiohttp.ThreadedResolver() requires a running event loop on
        # construction.
        self._session = AiohttpSession()

        self.bot = Bot(
            token=token,
            session=self._session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher()
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._log = logger.bind(component="telegram")

        self._wire_handlers()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the polling task. Idempotent."""
        if self._poll_task is not None:
            return
        # Inject the ThreadedResolver now that we're inside a running loop.
        self._session._connector_init.setdefault(
            "resolver", aiohttp.ThreadedResolver()
        )
        # Confirm credentials early — `get_me` fails fast on bad token.
        try:
            me = await self.bot.get_me()
            self._log.info("connected as @{} (id={})", me.username, me.id)
        except TelegramAPIError as e:
            self._log.error("Telegram auth failed: {}", e)
            raise

        self._poll_task = asyncio.create_task(self._poll_forever(), name="telegram_poll")

    async def stop(self) -> None:
        if self._poll_task is not None:
            await self.dispatcher.stop_polling()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._poll_task = None
        # Cancel any pending approval futures so awaiting callers don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()
        await self.bot.session.close()
        self._log.info("stopped")

    async def _poll_forever(self) -> None:
        """Run the dispatcher with backoff on crash. A network blip should
        never take down the whole process."""
        attempt = 0
        while True:
            try:
                await self.dispatcher.start_polling(self.bot)
                return  # graceful shutdown
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(60.0, 2.0**attempt)
                self._log.exception("polling crashed (attempt {}): {} — restart in {}s",
                                    attempt, e, delay)
                await asyncio.sleep(delay)

    # ── Notifier protocol ─────────────────────────────────────────────

    async def send_signal(self, signal: Signal) -> None:
        try:
            await self.bot.send_message(self.chat_id, format_signal(signal))
        except TelegramAPIError as e:
            self._log.warning("send_signal failed: {}", e)

    async def send_notice(self, text: str) -> None:
        try:
            await self.bot.send_message(self.chat_id, _escape(text))
        except TelegramAPIError as e:
            self._log.warning("send_notice failed: {}", e)

    async def request_approval(self, signal: Signal, timeout_s: float) -> bool:
        """Send the signal with ✅/❌ buttons; wait up to `timeout_s`. Returns
        False on timeout, rejection, or send failure."""
        approval_id = _secrets.token_urlsafe(8)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = fut
        try:
            text = format_signal(signal) + (
                f"\n\n<i>Approval window: {int(timeout_s)}s</i>"
            )
            try:
                await self.bot.send_message(
                    self.chat_id, text, reply_markup=approval_keyboard(approval_id)
                )
            except TelegramAPIError as e:
                self._log.warning("approval send failed: {}", e)
                return False
            try:
                return await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                self._log.info("approval timed out for {}", signal.symbol)
                return False
        finally:
            self._pending.pop(approval_id, None)

    # ── handlers wiring ───────────────────────────────────────────────

    def _wire_handlers(self) -> None:
        # Import here to avoid a circular import at module load time.
        from cero.ui.telegram.handlers import register

        register(self.dispatcher, self.services, self.allowed_chat_ids)

        # The approve/reject callbacks live here because they need direct
        # access to the pending futures.
        @self.dispatcher.callback_query(lambda cq: cq.data and cq.data.startswith(("approve:", "reject:")))
        async def _on_callback(cq: CallbackQuery) -> None:  # noqa: ANN202
            if str(cq.from_user.id) not in self.allowed_chat_ids:
                await cq.answer("not authorized", show_alert=True)
                return
            verb, _, approval_id = cq.data.partition(":")
            fut = self._pending.get(approval_id)
            if fut is None or fut.done():
                await cq.answer("already handled or expired")
                return
            approved = verb == "approve"
            fut.set_result(approved)
            await cq.answer("approved ✅" if approved else "rejected ❌")
            # Update the original message so the user can see the outcome.
            try:
                if cq.message is not None:
                    new_text = cq.message.html_text + (
                        f"\n\n<b>{'✅ APPROVED' if approved else '❌ REJECTED'}</b>"
                        f" by @{cq.from_user.username or cq.from_user.id}"
                    )
                    await cq.message.edit_text(new_text)
            except TelegramAPIError as e:
                self._log.debug("could not edit approval message: {}", e)


# ──────────────────────────────────────────────────────────────────────
# Convenience factory
# ──────────────────────────────────────────────────────────────────────


def build_notifier(
    token: str,
    chat_id: str,
    *,
    services: Optional[dict] = None,
    backup_chat_id: str = "",
) -> Optional[TelegramNotifier]:
    """Construct a TelegramNotifier if credentials are present, else None.
    Callers can fall back to LogNotifier so the rest of the system runs
    even without Telegram configured."""
    if not token or not chat_id:
        logger.warning("Telegram disabled: token or chat_id missing in .env")
        return None
    allowed = [chat_id] + ([backup_chat_id] if backup_chat_id else [])
    return TelegramNotifier(token, chat_id, services=services, allowed_chat_ids=allowed)
