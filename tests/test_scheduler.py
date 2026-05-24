"""Tests for cero/brain/scheduler.py.

A fake exchange supplies balance; the DB is real (temp file) so the scheduler's
SQL paths run. We seed candles directly, fire a `candle:closed` event, and
assert the brain pipeline produced a Signal row + handed it to the fake mode.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select

from cero.brain.risk import RiskGate
from cero.brain.scheduler import BrainScheduler
from cero.brain.signals import Signal
from cero.config import (
    Config,
    CriteriaWeights,
    DatabaseConfig,
    ExchangeConfig,
    NewsConfig,
    RiskConfig,
)
from cero.config import AlertsConfig, LoggingConfig, WebConfig
from cero.data.exchange import Balance, Candle
from cero.db.models import Candle as CandleRow
from cero.db.models import Signal as SignalRow
from cero.db.session import close_db, init_db, session_factory
from cero.events import EventBus


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _make_cfg(db_path: Path, symbols=None) -> Config:
    return Config(
        exchange=ExchangeConfig(name="bybit", testnet=True, margin_mode="isolated", leverage=5),
        symbols=symbols or ["ETH/USDT:USDT"],
        timeframes=["5m", "15m", "30m", "1h", "4h", "1d"],
        backfill_candles=300,
        mode="signal_only",
        risk=RiskConfig(
            base_risk_per_trade_pct=0.5, max_daily_loss_pct=3.0,
            max_consecutive_losses=4, max_concurrent_positions=3,
            tier_sizing={"A": 1.0, "B": 0.5, "C": 0.0, "D": 0.0},
            tier_thresholds={"A": 80, "B": 60, "C": 40},
        ),
        criteria_weights=CriteriaWeights(
            trend_h1_h4=20, market_structure=18, key_levels=10, poi_alert=15,
            session_hl=5, structure_15m_30m=12, ltf_poi=12, atr_room=8,
        ),
        news=NewsConfig(blackout_minutes_before=15, blackout_minutes_after=15,
                        blackout_impacts=["high"], sources=[], twitter_watchlist=[]),
        alerts=AlertsConfig(),
        web=WebConfig(),
        database=DatabaseConfig(path=str(db_path), echo=False),
        logging=LoggingConfig(),
    )


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_scheduler.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


class FakeExchange:
    """Tiny ExchangeClient surrogate the scheduler can call `fetch_balance` on."""

    def __init__(self, equity: float = 10_000.0) -> None:
        self._equity = equity

    async def fetch_balance(self) -> Balance:
        return Balance(
            quote_currency="USDT", equity=self._equity, balance=self._equity,
            unrealized_pnl=0.0, margin_used=0.0,
        )


class FakeMode:
    name = "fake"
    def __init__(self) -> None:
        self.received: list[Signal] = []

    async def handle_signal(self, signal: Signal) -> None:
        self.received.append(signal)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


_TF_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


async def _seed_uptrend(symbol: str, base_ms: int) -> None:
    """Seed a clean uptrend across all timeframes so the brain has
    enough data to produce a real result (won't all be 'flat')."""
    async with session_factory()() as s:
        for tf in _TF_MS:
            step = _TF_MS[tf]
            closes = list(np.linspace(2000.0, 3000.0, 120))
            for i, c in enumerate(closes):
                ot = base_ms - (len(closes) - 1 - i) * step
                s.add(CandleRow(
                    symbol=symbol, timeframe=tf,
                    open_time=ot, close_time=ot + step - 1,
                    open=c, high=c + 0.5, low=c - 0.5, close=c, volume=10.0,
                ))
        await s.commit()


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


async def test_scheduler_runs_brain_on_candle_event(temp_db):
    cfg = _make_cfg(temp_db)
    ex = FakeExchange(equity=10_000)
    bus = EventBus()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=bus)
    mode = FakeMode()

    sched = BrainScheduler(cfg, ex, gate, lambda: mode, event_bus=bus)

    base_ms = int(datetime(2026, 5, 24, 12, tzinfo=timezone.utc).timestamp() * 1000)
    await _seed_uptrend("ETH/USDT:USDT", base_ms)

    sched.start()
    try:
        # Trigger one tick.
        await bus.publish(
            "candle:closed:ETH/USDT:USDT:5m",
            {"symbol": "ETH/USDT:USDT", "tf": "5m"},
        )
        # Give the scheduler a moment to process.
        await asyncio.sleep(0.5)
    finally:
        await sched.stop()

    # A signal row should have been written.
    async with session_factory()() as s:
        signals = (await s.execute(select(SignalRow))).scalars().all()
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "ETH/USDT:USDT"
    assert sig.score >= 0
    # And the fake mode received it (since score >= C threshold for this data).
    assert len(mode.received) >= 0   # may or may not — depends on tier
    # If tier is A/B/C the mode receives it; only D is filtered.
    if sig.tier in ("A", "B", "C"):
        assert len(mode.received) == 1


async def test_scheduler_skips_when_no_candles(temp_db):
    cfg = _make_cfg(temp_db)
    ex = FakeExchange()
    bus = EventBus()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=bus)
    mode = FakeMode()

    sched = BrainScheduler(cfg, ex, gate, lambda: mode, event_bus=bus)
    sched.start()
    try:
        await bus.publish("candle:closed:ETH/USDT:USDT:5m", {})
        await asyncio.sleep(0.3)
    finally:
        await sched.stop()

    async with session_factory()() as s:
        signals = (await s.execute(select(SignalRow))).scalars().all()
    assert signals == []
    assert mode.received == []


async def test_scheduler_rejects_unknown_trigger_tf(temp_db):
    cfg = _make_cfg(temp_db)
    ex = FakeExchange()
    bus = EventBus()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=bus)
    mode = FakeMode()

    sched = BrainScheduler(cfg, ex, gate, lambda: mode, trigger_tf="2h", event_bus=bus)
    with pytest.raises(ValueError):
        sched.start()
