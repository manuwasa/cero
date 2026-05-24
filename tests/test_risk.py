"""Tests for cero/brain/risk.py.

Two layers:
  - Pure-function tests (position_size, today_realized_pnl, consecutive_losses,
    in_news_blackout): no DB, no async.
  - RiskGate tests: in-memory state machine + a temp SQLite DB for trip
    persistence. Hits init_db / session_factory through the real engine.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cero.brain.risk import (
    RiskGate,
    consecutive_losses,
    in_news_blackout,
    position_size,
    today_realized_pnl,
)
from cero.config import DatabaseConfig, NewsConfig, RiskConfig
from cero.db.models import TripEvent
from cero.db.session import close_db, init_db, session_factory
from sqlalchemy import select


# ──────────────────────────────────────────────────────────────────────
# Fixtures
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
    blackout_minutes_before=15,
    blackout_minutes_after=15,
    blackout_impacts=["high", "medium"],
    sources=[],
    twitter_watchlist=[],
)


@pytest_asyncio.fixture
async def temp_db():
    """Spin up a throwaway SQLite for tests that touch the DB."""
    tmp = Path(tempfile.gettempdir()) / "cero_test_risk.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


def _ms(year, month, day, hour=12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


# ──────────────────────────────────────────────────────────────────────
# position_size
# ──────────────────────────────────────────────────────────────────────


def test_position_size_basic_math():
    # 10,000 equity * 0.5% risk * 1.0 tier = $50 risk
    # $50 / $25 stop = 2 contracts
    size = position_size(
        equity=10_000, base_risk_pct=0.5, tier_multiplier=1.0, stop_distance=25
    )
    assert size == pytest.approx(2.0)


def test_position_size_tier_b_halves():
    # Same setup, tier B (0.5x) → 1 contract
    size = position_size(
        equity=10_000, base_risk_pct=0.5, tier_multiplier=0.5, stop_distance=25
    )
    assert size == pytest.approx(1.0)


@pytest.mark.parametrize(
    "equity,stop", [(0, 25), (10_000, 0), (-1, 25), (10_000, -5)]
)
def test_position_size_zero_on_invalid_inputs(equity, stop):
    assert position_size(
        equity=equity, base_risk_pct=0.5, tier_multiplier=1.0, stop_distance=stop
    ) == 0.0


# ──────────────────────────────────────────────────────────────────────
# today_realized_pnl + consecutive_losses
# ──────────────────────────────────────────────────────────────────────


def test_today_pnl_sums_only_todays_trades():
    now = _ms(2026, 5, 24)
    yesterday = _ms(2026, 5, 23)
    trades = [
        SimpleNamespace(closed_at=yesterday, realized_pnl=-100.0),
        SimpleNamespace(closed_at=now,        realized_pnl=-25.0),
        SimpleNamespace(closed_at=now,        realized_pnl=15.0),
    ]
    assert today_realized_pnl(trades, now_ms=now) == pytest.approx(-10.0)


def test_consecutive_losses_counts_from_end():
    trades = [
        SimpleNamespace(realized_pnl=10.0),    # win
        SimpleNamespace(realized_pnl=-5.0),    # loss
        SimpleNamespace(realized_pnl=-3.0),    # loss
        SimpleNamespace(realized_pnl=-1.0),    # loss   ← streak of 3
    ]
    assert consecutive_losses(trades) == 3


def test_consecutive_losses_broken_by_breakeven():
    trades = [
        SimpleNamespace(realized_pnl=-5.0),
        SimpleNamespace(realized_pnl=0.0),     # breakeven breaks the streak
        SimpleNamespace(realized_pnl=-1.0),    # loss   ← streak of 1
    ]
    assert consecutive_losses(trades) == 1


# ──────────────────────────────────────────────────────────────────────
# in_news_blackout
# ──────────────────────────────────────────────────────────────────────


def test_blackout_active_inside_window():
    now = _ms(2026, 5, 24, hour=14)
    e = SimpleNamespace(ts=now + 5 * 60_000, impact="high", name="CPI")   # 5 min away
    active, name = in_news_blackout([e], now, NEWS)
    assert active is True
    assert name == "CPI"


def test_blackout_inactive_outside_window():
    now = _ms(2026, 5, 24, hour=14)
    e = SimpleNamespace(ts=now + 60 * 60_000, impact="high", name="CPI")  # 60 min away
    active, _ = in_news_blackout([e], now, NEWS)
    assert active is False


def test_blackout_ignores_low_impact():
    now = _ms(2026, 5, 24, hour=14)
    e = SimpleNamespace(ts=now, impact="low", name="Random")
    active, _ = in_news_blackout([e], now, NEWS)
    assert active is False


# ──────────────────────────────────────────────────────────────────────
# RiskGate.size_for — gate ordering
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(RISK, NEWS)


def _default_kwargs(**over):
    base = dict(
        equity=10_000,
        tier_multiplier=1.0,
        stop_distance=25.0,
        open_positions=0,
        today_realized=0.0,
        today_consecutive_losses=0,
        in_blackout=False,
    )
    base.update(over)
    return base


def test_gate_returns_size_when_all_clear(gate):
    d = gate.size_for(**_default_kwargs())
    assert d.size == pytest.approx(2.0)
    assert d.blocked_by is None


def test_gate_blocks_when_tier_is_zero(gate):
    d = gate.size_for(**_default_kwargs(tier_multiplier=0.0))
    assert d.size == 0.0
    assert d.blocked_by == "tier"


def test_gate_blocks_when_no_stop(gate):
    d = gate.size_for(**_default_kwargs(stop_distance=None))
    assert d.blocked_by == "no_stop"


def test_gate_blocks_on_max_concurrent_positions(gate):
    d = gate.size_for(**_default_kwargs(open_positions=3))
    assert d.blocked_by == "concurrent_positions"


def test_gate_blocks_on_daily_loss_cap(gate):
    # 3% of 10k = $300 loss → at cap
    d = gate.size_for(**_default_kwargs(today_realized=-300.0))
    assert d.blocked_by == "daily_loss"


def test_gate_blocks_on_consecutive_losses(gate):
    d = gate.size_for(**_default_kwargs(today_consecutive_losses=4))
    assert d.blocked_by == "consecutive_losses"


def test_gate_blocks_on_news_blackout(gate):
    d = gate.size_for(**_default_kwargs(in_blackout=True, blackout_name="CPI"))
    assert d.blocked_by == "news_blackout"
    assert "CPI" in d.reason


# ──────────────────────────────────────────────────────────────────────
# RiskGate trip / reset / hydrate — requires a DB
# ──────────────────────────────────────────────────────────────────────


async def test_trip_persists_and_blocks(temp_db):
    gate = RiskGate(RISK, NEWS)
    assert gate.tripped is False
    trip_id = await gate.trip("daily_loss", "test")
    assert trip_id > 0
    assert gate.tripped is True
    assert gate.trip_reason == "daily_loss"

    d = gate.size_for(**_default_kwargs())
    assert d.size == 0.0
    assert d.blocked_by == "tripped"

    # Verify DB row
    async with session_factory()() as s:
        rows = (await s.execute(select(TripEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].cleared_at is None


async def test_double_trip_is_idempotent(temp_db):
    gate = RiskGate(RISK, NEWS)
    await gate.trip("manual", "first")
    result = await gate.trip("daily_loss", "second")   # should be ignored
    assert result == -1
    # Only one row in DB
    async with session_factory()() as s:
        rows = (await s.execute(select(TripEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == "manual"


async def test_reset_clears_row_and_unblocks(temp_db):
    gate = RiskGate(RISK, NEWS)
    await gate.trip("manual", "test")
    cleared = await gate.reset(by="user")
    assert cleared is True
    assert gate.tripped is False

    async with session_factory()() as s:
        row = (await s.execute(select(TripEvent))).scalar_one()
    assert row.cleared_at is not None
    assert row.cleared_by == "user"


async def test_hydrate_picks_up_existing_trip(temp_db):
    # First gate trips
    g1 = RiskGate(RISK, NEWS)
    await g1.trip("consecutive_losses", "simulated")

    # Second gate (fresh process) should detect the un-cleared row
    g2 = RiskGate(RISK, NEWS)
    assert g2.tripped is False
    await g2.hydrate()
    assert g2.tripped is True
    assert g2.trip_reason == "consecutive_losses"


# ──────────────────────────────────────────────────────────────────────
# evaluate_trip_triggers — pure / synchronous
# ──────────────────────────────────────────────────────────────────────


def test_evaluate_triggers_fires_on_daily_loss(gate):
    reason, _ = gate.evaluate_trip_triggers(
        equity=10_000, today_realized=-310.0, today_consecutive_losses=0
    )
    assert reason == "daily_loss"


def test_evaluate_triggers_fires_on_consecutive_losses(gate):
    reason, _ = gate.evaluate_trip_triggers(
        equity=10_000, today_realized=-100.0, today_consecutive_losses=4
    )
    assert reason == "consecutive_losses"


def test_evaluate_triggers_quiet_when_safe(gate):
    reason, _ = gate.evaluate_trip_triggers(
        equity=10_000, today_realized=50.0, today_consecutive_losses=1
    )
    assert reason is None
