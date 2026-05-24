"""Tests for cero/ui/telegram/bot.py.

We don't talk to real Telegram here — that's the smoke test's job. These cover:
  - format_signal output shape + escaping
  - approval_keyboard callback_data shape
  - request_approval state machine (pending future resolution, timeout, cleanup)

The state-machine tests bypass the dispatcher entirely by manually resolving
the pending futures.
"""
from __future__ import annotations

import asyncio

import pytest

from cero.brain.signals import Signal
from cero.ui.telegram.bot import (
    approval_keyboard,
    format_signal,
    TelegramNotifier,
    _escape,
)


def _signal(**over) -> Signal:
    base = dict(
        ts=0, symbol="ETH/USDT:USDT", tier="B", direction="long", score=67,
        size_multiplier=0.5, size=0.3125,
        entry_price=3000.0, stop_loss=2920.0, take_profit=3160.0,
        mode="approval", size_reason="ok",
    )
    base.update(over)
    return Signal(**base)


# ──────────────────────────────────────────────────────────────────────
# format_signal
# ──────────────────────────────────────────────────────────────────────


def test_format_signal_includes_core_fields():
    s = _signal()
    text = format_signal(s)
    for needle in ("ETH/USDT:USDT", "Tier B", "long", "67/100",
                   "3000.00", "2920.00", "3160.00", "0.312500"):
        assert needle in text, f"missing {needle!r} in:\n{text}"


def test_format_signal_actionable_vs_informational():
    actionable = format_signal(_signal())
    info = format_signal(_signal(size=0.0, direction="none"))
    assert "actionable" in actionable
    assert "informational" in info


def test_escape_sanitizes_html_chars():
    assert _escape("a & b <c>") == "a &amp; b &lt;c&gt;"


def test_escape_in_size_reason_passes_through_format():
    # Size reason containing HTML must not break the rendered message.
    s = _signal(size=0.0, size_reason="<dangerous> & stuff")
    text = format_signal(s)
    assert "&lt;dangerous&gt; &amp; stuff" in text


# ──────────────────────────────────────────────────────────────────────
# approval_keyboard
# ──────────────────────────────────────────────────────────────────────


def test_approval_keyboard_has_two_buttons_with_correct_callbacks():
    kb = approval_keyboard("abc123")
    assert len(kb.inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert len(row) == 2
    assert row[0].callback_data == "approve:abc123"
    assert row[1].callback_data == "reject:abc123"


# ──────────────────────────────────────────────────────────────────────
# request_approval — bypass the dispatcher by patching send_message
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def notifier(monkeypatch):
    """Construct a TelegramNotifier without actually contacting Telegram.
    `bot.send_message` is stubbed; `bot.session.close` is a no-op."""
    n = TelegramNotifier("123:fake", chat_id="42", allowed_chat_ids=["42"])

    sent: list[dict] = []

    async def fake_send(chat_id, text, **kw):
        sent.append({"chat_id": chat_id, "text": text, "kw": kw})

    async def fake_close():
        pass

    monkeypatch.setattr(n.bot, "send_message", fake_send)
    monkeypatch.setattr(n.bot.session, "close", fake_close)
    n._sent = sent  # type: ignore[attr-defined]   tests inspect this
    return n


async def test_request_approval_resolves_to_true_when_future_set(notifier):
    async def approve_soon():
        await asyncio.sleep(0.05)
        # Find the one pending future and set it True
        assert len(notifier._pending) == 1
        for fut in notifier._pending.values():
            fut.set_result(True)

    asyncio.create_task(approve_soon())
    result = await notifier.request_approval(_signal(), timeout_s=2.0)
    assert result is True
    # Pending dict cleans up on return
    assert notifier._pending == {}


async def test_request_approval_resolves_to_false_when_rejected(notifier):
    async def reject_soon():
        await asyncio.sleep(0.05)
        for fut in notifier._pending.values():
            fut.set_result(False)

    asyncio.create_task(reject_soon())
    result = await notifier.request_approval(_signal(), timeout_s=2.0)
    assert result is False


async def test_request_approval_times_out(notifier):
    result = await notifier.request_approval(_signal(), timeout_s=0.1)
    assert result is False
    assert notifier._pending == {}   # cleaned up even on timeout


async def test_request_approval_sends_keyboard(notifier):
    asyncio.create_task(_resolve_soon(notifier, True, 0.02))
    await notifier.request_approval(_signal(), timeout_s=1.0)
    assert len(notifier._sent) == 1   # type: ignore[attr-defined]
    sent = notifier._sent[0]          # type: ignore[attr-defined]
    assert sent["chat_id"] == "42"
    assert "reply_markup" in sent["kw"]


async def _resolve_soon(notifier: TelegramNotifier, value: bool, delay: float) -> None:
    await asyncio.sleep(delay)
    for fut in notifier._pending.values():
        if not fut.done():
            fut.set_result(value)
