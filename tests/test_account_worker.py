"""Tests for cero/data/account_worker.py.

We test the `_tick` reconciliation logic directly — no real exchange, no
running loop. A `FakeExchange` returns whatever balance/positions the test
sets up; we mutate that between ticks to simulate the exchange state changing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from cero.brain.risk import RiskGate
from cero.config import (
    AlertsConfig, Config, CriteriaWeights, DatabaseConfig, ExchangeConfig,
    LoggingConfig, NewsConfig, RiskConfig, WebConfig,
)
from cero.data.account_worker import AccountWorker
from cero.data.exchange import Balance, PositionInfo
from cero.db.models import AccountSnapshot, Position
from cero.db.session import close_db, init_db, session_factory
from cero.events import EventBus


def _cfg(db_path: Path, symbols=None) -> Config:
    return Config(
        exchange=ExchangeConfig(name="bybit", testnet=True),
        symbols=symbols or ["ETH/USDT:USDT"],
        timeframes=["5m", "1h"], backfill_candles=300,
        account_poll_seconds=10, mode="signal_only",
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
        alerts=AlertsConfig(), web=WebConfig(),
        database=DatabaseConfig(path=str(db_path), echo=False),
        logging=LoggingConfig(),
    )


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_acct.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


class FakeExchange:
    def __init__(self) -> None:
        self.balance = Balance(
            quote_currency="USDT", equity=10_000.0, balance=10_000.0,
            unrealized_pnl=0.0, margin_used=0.0,
        )
        self.positions: list[PositionInfo] = []

    async def fetch_balance(self) -> Balance:
        return self.balance

    async def fetch_positions(self, symbols=None) -> list[PositionInfo]:
        return list(self.positions)


def _pinfo(symbol: str, side: str = "long", size: float = 0.5,
           pid: str | None = "p1") -> PositionInfo:
    return PositionInfo(
        symbol=symbol, side=side,
        size=size if side == "long" else -size,
        entry_price=3000.0, mark_price=3050.0, leverage=5,
        unrealized_pnl=25.0,
        exchange_position_id=pid,
    )


# ──────────────────────────────────────────────────────────────────────
# Balance snapshotting
# ──────────────────────────────────────────────────────────────────────


async def test_tick_writes_account_snapshot(temp_db):
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()
    async with session_factory()() as s:
        rows = (await s.execute(select(AccountSnapshot))).scalars().all()
    assert len(rows) == 1
    assert rows[0].equity == pytest.approx(10_000.0)


# ──────────────────────────────────────────────────────────────────────
# Position reconciliation
# ──────────────────────────────────────────────────────────────────────


async def test_first_tick_imports_existing_positions_without_tripping(temp_db):
    """When the worker starts and finds positions on the exchange that aren't
    in our DB, that's the post-restart case — import silently, don't trip."""
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    ex.positions = [_pinfo("ETH/USDT:USDT", pid="px-1")]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()

    assert gate.tripped is False
    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
    assert len(rows) == 1
    assert rows[0].exchange_position_id == "px-1"


async def test_unexpected_position_on_second_tick_trips(temp_db):
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    # First tick — empty exchange, empty DB
    await w._tick()
    assert gate.tripped is False

    # Manual trade appears on the exchange between ticks
    ex.positions = [_pinfo("BTC/USDT:USDT", side="short", pid="manual-1")]
    await w._tick()

    assert gate.tripped is True
    assert gate.trip_reason == "unexpected_position"
    assert "BTC/USDT:USDT" in gate.trip_detail


async def test_position_updates_propagate(temp_db):
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    ex.positions = [_pinfo("ETH/USDT:USDT", pid="p-up")]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()
    # Mark price moves, uPnL changes.
    new = _pinfo("ETH/USDT:USDT", pid="p-up")
    new = new.model_copy(update={"mark_price": 3100.0, "unrealized_pnl": 50.0})
    ex.positions = [new]
    await w._tick()

    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
    assert len(rows) == 1
    assert rows[0].mark_price == pytest.approx(3100.0)
    assert rows[0].unrealized_pnl == pytest.approx(50.0)


async def test_position_closure_removes_row(temp_db):
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    ex.positions = [_pinfo("ETH/USDT:USDT", pid="p-close")]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()        # import
    ex.positions = []      # exchange now flat
    await w._tick()

    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
    assert rows == []


async def test_position_closure_writes_trade_row(temp_db):
    """Bug 2 regression: when a position disappears from the exchange, we
    must create a Trade row so PnL stats include it (otherwise the
    validation gate never counts any trades)."""
    from cero.db.models import Trade
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    # Long position at entry 3000, currently marked at 3100 (winning).
    ex.positions = [PositionInfo(
        symbol="ETH/USDT:USDT", side="long", size=0.5,
        entry_price=3000.0, mark_price=3100.0, leverage=5.0,
        unrealized_pnl=50.0, exchange_position_id=None,
    )]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()          # import
    ex.positions = []        # closed externally (SL/TP/manual)
    await w._tick()          # reconcile detects the closure

    async with session_factory()() as s:
        trades = (await s.execute(select(Trade))).scalars().all()
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "ETH/USDT:USDT"
    assert t.side == "long"
    assert t.size == pytest.approx(0.5)
    assert t.entry_price == pytest.approx(3000.0)
    assert t.exit_price == pytest.approx(3100.0)
    # long: (exit - entry) * size = (3100 - 3000) * 0.5 = 50
    assert t.realized_pnl == pytest.approx(50.0)
    assert t.exit_reason == "other"


async def test_position_closure_writes_trade_row_short_loss(temp_db):
    """Short side + losing trade — verifies sign convention."""
    from cero.db.models import Trade
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    # Short 0.5 (signed -0.5) at entry 3000, marked at 3100 → loss.
    ex.positions = [PositionInfo(
        symbol="ETH/USDT:USDT", side="short", size=-0.5,
        entry_price=3000.0, mark_price=3100.0, leverage=5.0,
        unrealized_pnl=-50.0, exchange_position_id=None,
    )]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()
    ex.positions = []
    await w._tick()

    async with session_factory()() as s:
        trades = (await s.execute(select(Trade))).scalars().all()
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "short"
    assert t.size == pytest.approx(0.5)   # stored as absolute
    # short: (entry - exit) * |size| = (3000 - 3100) * 0.5 = -50
    assert t.realized_pnl == pytest.approx(-50.0)


async def test_no_id_falls_back_to_symbol_side_key(temp_db):
    """Some exchanges don't return a stable position id. We should still
    reconcile using symbol+side as a fallback key."""
    cfg = _cfg(temp_db)
    ex = FakeExchange()
    ex.positions = [_pinfo("ETH/USDT:USDT", pid=None)]
    gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
    w = AccountWorker(cfg, ex, gate)

    await w._tick()
    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
    assert len(rows) == 1

    # Same symbol+side reappears (with updated mark) — should update, not insert.
    new = _pinfo("ETH/USDT:USDT", pid=None).model_copy(update={"mark_price": 3200.0})
    ex.positions = [new]
    await w._tick()
    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
    assert len(rows) == 1
    assert rows[0].mark_price == pytest.approx(3200.0)
