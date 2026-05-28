"""Tests for cero/brain/signals.py + cero/exec/modes.py.

No exchange, no DB except where required (TripWatcher end-to-end test uses
the bus but not the DB). Mocks satisfy the Notifier / OrderPlacer protocols.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

from cero.brain.criteria import CriterionResult, MarketContext
from cero.brain.risk import RiskGate
from cero.brain.scoring import ScoreReport
from cero.brain.signals import Signal, build_signal
from cero.config import DatabaseConfig, NewsConfig, RiskConfig
from cero.data.exchange import Candle
from cero.db.session import close_db, init_db
from cero.events import EventBus
from cero.exec.modes import (
    ApprovalMode,
    AutoMode,
    LogNotifier,
    SignalOnlyMode,
    StubOrderPlacer,
    TripWatcher,
    build_mode,
)


# ──────────────────────────────────────────────────────────────────────
# Common fixtures
# ──────────────────────────────────────────────────────────────────────


RISK = RiskConfig(
    base_risk_per_trade_pct=0.5,
    max_daily_loss_pct=3.0,
    max_consecutive_losses=4,
    max_concurrent_positions=3,
    tier_sizing={"A": 1.0, "B": 0.5, "C": 0.0, "D": 0.0},
    tier_thresholds={"A": 80, "B": 60, "C": 40},
)
NEWS = NewsConfig(
    blackout_minutes_before=15, blackout_minutes_after=15,
    blackout_impacts=["high"], sources=[], twitter_watchlist=[],
)


def _bar(close: float, ts: int) -> Candle:
    return Candle(
        symbol="ETH/USDT:USDT", timeframe="1h",
        open_time=ts, open=close, high=close + 1, low=close - 1,
        close=close, volume=10.0,
    )


def _ctx(price: float = 3000.0) -> MarketContext:
    now = int(datetime(2026, 5, 24, 12, tzinfo=timezone.utc).timestamp() * 1000)
    bars = [_bar(price, now - (100 - i) * 3_600_000) for i in range(100)]
    from cero.brain.criteria import MarketContext  # local to avoid circular noise
    return MarketContext(
        symbol="ETH/USDT:USDT", now_ms=now, candles={"1h": bars},
        weights=__import__("cero.config", fromlist=["CriteriaWeights"]).CriteriaWeights(
            trend_h1_h4=20, market_structure=18, key_levels=10, poi_alert=15,
            session_hl=5, structure_15m_30m=12, ltf_poi=12, atr_room=8,
        ),
        round_step=100.0,
    )


def _report(
    score: int = 67, tier: str = "B", direction: str = "long",
    size_multiplier: float = 0.5,
) -> ScoreReport:
    results = [
        CriterionResult(name="trend_h1_h4", weight=20, passed=True, detail="up",
                        direction_hint="up" if direction == "long" else "down"),
        CriterionResult(name="poi_alert", weight=15, passed=True, detail="in OTE"),
    ]
    return ScoreReport(
        score=score, tier=tier, direction=direction,
        size_multiplier=size_multiplier,
        passed=[r.name for r in results], failed=[], results=results,
    )


# ──────────────────────────────────────────────────────────────────────
# Notifier / OrderPlacer test doubles
# ──────────────────────────────────────────────────────────────────────


class FakeNotifier:
    def __init__(self, approve: bool = True) -> None:
        self.signals: list[Signal] = []
        self.notices: list[str] = []
        self.approval_requests: list[tuple[Signal, float]] = []
        self._approve = approve

    async def send_signal(self, signal: Signal) -> None:
        self.signals.append(signal)

    async def send_notice(self, text: str) -> None:
        self.notices.append(text)

    async def request_approval(self, signal: Signal, timeout_s: float) -> bool:
        self.approval_requests.append((signal, timeout_s))
        return self._approve


# ──────────────────────────────────────────────────────────────────────
# build_signal
# ──────────────────────────────────────────────────────────────────────


def test_build_signal_long_sl_below_tp_above():
    ctx = _ctx(price=3000.0)
    report = _report(direction="long")
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000.0, atr_h1=80.0, mode="signal_only",
    )
    assert s.direction == "long"
    assert s.entry_price == pytest.approx(3000.0)
    assert s.stop_loss == pytest.approx(2920.0)        # entry - 1 x ATR
    assert s.take_profit == pytest.approx(3160.0)      # entry + 2 x ATR
    assert s.stop_distance == pytest.approx(80.0)
    # B-tier (0.5x), 0.5% base risk, equity 10k → $25 risk; size = 25/80
    assert s.size == pytest.approx(25.0 / 80.0)
    assert s.is_actionable is True


def test_build_signal_short_mirrored():
    ctx = _ctx(price=3000.0)
    report = _report(direction="short")
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000.0, atr_h1=80.0, mode="signal_only",
    )
    assert s.stop_loss == pytest.approx(3080.0)
    assert s.take_profit == pytest.approx(2840.0)


def test_build_signal_none_direction_yields_zero_size():
    ctx = _ctx()
    report = _report(direction="none", tier="A", size_multiplier=1.0, score=100)
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000, atr_h1=80.0, mode="signal_only",
    )
    assert s.size == 0
    assert s.is_actionable is False


def test_build_signal_clamps_max_when_atr_too_large_for_price():
    """For low-priced volatile coins like SOL where ATR(H1) is huge relative
    to price, the SL/TP must clamp to MAX_STOP_PCT instead of producing a
    nonsensical (potentially negative) target."""
    from cero.brain.signals import MAX_STOP_PCT
    ctx = _ctx(price=80.0)
    ctx.candles["1h"][-1] = ctx.candles["1h"][-1].model_copy(update={"close": 80.0})
    report = _report(direction="short")
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000.0, atr_h1=50.0,   # raw_pct = 62.5% → clamped to 3%
        mode="signal_only",
    )
    expected_stop = 80.0 * MAX_STOP_PCT
    # short: SL above, TP below
    assert s.stop_loss == pytest.approx(80.0 + expected_stop)
    assert s.take_profit == pytest.approx(80.0 - 2 * expected_stop)
    # TP must remain positive — that was the SOL bug
    assert s.take_profit > 0


def test_build_signal_clamps_min_when_atr_too_small():
    """In a quiet market, ATR can collapse near zero; the stop must still
    be wide enough to clear typical noise — clamp up to MIN_STOP_PCT."""
    from cero.brain.signals import MIN_STOP_PCT
    ctx = _ctx(price=3000.0)
    report = _report(direction="long")
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000.0, atr_h1=1.0,    # raw_pct = 0.033% → clamped to 0.3%
        mode="signal_only",
    )
    expected_stop = 3000.0 * MIN_STOP_PCT
    assert s.stop_loss == pytest.approx(3000.0 - expected_stop)
    assert s.take_profit == pytest.approx(3000.0 + 2 * expected_stop)


def test_build_signal_tripped_gate_yields_zero_size():
    ctx = _ctx()
    report = _report(direction="long")
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    # Manually flip the trip state (bypass DB by not calling .trip())
    gate._tripped = True
    gate._trip_reason = "manual"
    gate._trip_detail = "test"
    s = build_signal(
        ctx=ctx, report=report, risk_gate=gate,
        equity=10_000, atr_h1=80.0, mode="signal_only",
    )
    assert s.size == 0
    assert "tripped" in s.size_reason.lower() or "TRIPPED" in s.size_reason


# ──────────────────────────────────────────────────────────────────────
# Mode behaviors
# ──────────────────────────────────────────────────────────────────────


def _signal(
    tier: str = "B", direction: str = "long", size: float = 0.3125,
    size_reason: str = "ok",
) -> Signal:
    return Signal(
        ts=0, symbol="ETH/USDT:USDT", tier=tier, direction=direction, score=67,
        size_multiplier=0.5, size=size,
        entry_price=3000.0, stop_loss=2920.0, take_profit=3160.0,
        mode="test", size_reason=size_reason,
    )


async def test_signal_only_mode_only_notifies():
    notif = FakeNotifier()
    mode = SignalOnlyMode(notifier=notif)
    await mode.handle_signal(_signal())
    assert len(notif.signals) == 1
    # Non-actionable signals are filtered out — kills Tier C/D spam.
    await mode.handle_signal(_signal(size=0, size_reason="tier=0"))
    assert len(notif.signals) == 1


async def test_approval_mode_places_on_approval():
    notif = FakeNotifier(approve=True)
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    mode = ApprovalMode(notifier=notif, placer=placer, risk_gate=gate, timeout_s=1.0)
    await mode.handle_signal(_signal())
    assert len(notif.approval_requests) == 1
    assert len(placer.placed) == 1


async def test_approval_mode_skips_when_rejected():
    notif = FakeNotifier(approve=False)
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    mode = ApprovalMode(notifier=notif, placer=placer, risk_gate=gate, timeout_s=1.0)
    await mode.handle_signal(_signal())
    assert len(notif.approval_requests) == 1
    assert len(placer.placed) == 0


async def test_approval_mode_skips_non_actionable_without_asking():
    notif = FakeNotifier(approve=True)
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    mode = ApprovalMode(notifier=notif, placer=placer, risk_gate=gate)
    await mode.handle_signal(_signal(size=0, size_reason="tier=0"))
    assert len(notif.approval_requests) == 0
    assert len(notif.signals) == 1
    assert len(placer.placed) == 0


async def test_approval_mode_blocked_when_tripped():
    notif = FakeNotifier(approve=True)
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    gate._tripped = True
    gate._trip_reason = "manual"
    mode = ApprovalMode(notifier=notif, placer=placer, risk_gate=gate)
    await mode.handle_signal(_signal())
    assert len(notif.notices) == 1
    assert "TRIPPED" in notif.notices[0]
    assert len(placer.placed) == 0


async def test_auto_mode_places_on_actionable_ab():
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    mode = AutoMode(notifier=notif, placer=placer, risk_gate=gate)
    await mode.handle_signal(_signal(tier="A"))
    assert len(placer.placed) == 1


async def test_auto_mode_skips_c_tier():
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    mode = AutoMode(notifier=notif, placer=placer, risk_gate=gate)
    await mode.handle_signal(_signal(tier="C"))
    assert len(placer.placed) == 0


async def test_auto_mode_blocked_when_tripped():
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    gate._tripped = True
    mode = AutoMode(notifier=notif, placer=placer, risk_gate=gate)
    await mode.handle_signal(_signal(tier="A"))
    assert len(placer.placed) == 0


# ──────────────────────────────────────────────────────────────────────
# build_mode factory
# ──────────────────────────────────────────────────────────────────────


def test_build_mode_returns_each_type():
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    assert isinstance(
        build_mode("signal_only", notifier=notif, placer=placer, risk_gate=gate),
        SignalOnlyMode,
    )
    assert isinstance(
        build_mode("approval", notifier=notif, placer=placer, risk_gate=gate),
        ApprovalMode,
    )
    assert isinstance(
        build_mode("auto", notifier=notif, placer=placer, risk_gate=gate),
        AutoMode,
    )


def test_build_mode_rejects_unknown_name():
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    gate = RiskGate(RISK, NEWS, event_bus=EventBus())
    with pytest.raises(ValueError):
        build_mode("yolo", notifier=notif, placer=placer, risk_gate=gate)


# ──────────────────────────────────────────────────────────────────────
# TripWatcher end-to-end (with bus + DB)
# ──────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_modes.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


async def test_trip_watcher_cancels_and_closes_on_fire(temp_db):
    test_bus = EventBus()
    notif = FakeNotifier()
    placer = StubOrderPlacer()
    symbols = ["ETH/USDT:USDT", "BTC/USDT:USDT"]

    watcher = TripWatcher(notif, placer, symbols, event_bus=test_bus)
    watcher.start()
    try:
        # Fire a trip through a RiskGate wired to the same bus.
        gate = RiskGate(RISK, NEWS, event_bus=test_bus)
        await gate.trip("daily_loss", "test fire")

        # Give the watcher one event-loop turn to process.
        await asyncio.sleep(0.05)

        assert set(placer.canceled) == set(symbols)
        assert set(placer.closed) == set(symbols)
        assert any("TRIPPED" in n for n in notif.notices)
    finally:
        await watcher.stop()
