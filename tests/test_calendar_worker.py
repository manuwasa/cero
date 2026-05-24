"""Tests for cero/data/calendar_worker.py.

Parse tests use small JSON fixtures. The worker tests inject a fake fetcher
so we never hit the real network. Blackout tests seed events into a temp DB
and assert the helper picks them up.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from cero.config import (
    AlertsConfig, Config, CriteriaWeights, DatabaseConfig, ExchangeConfig,
    LoggingConfig, NewsConfig, RiskConfig, WebConfig,
)
from cero.data.calendar_worker import (
    CalendarWorker,
    current_blackout,
    parse_feed,
)
from cero.db.models import CalendarEvent
from cero.db.session import close_db, init_db, session_factory


def _cfg(db_path: Path) -> Config:
    return Config(
        exchange=ExchangeConfig(name="bybit", testnet=True),
        symbols=["ETH/USDT:USDT"], timeframes=["5m", "1h"],
        backfill_candles=300, account_poll_seconds=10, mode="signal_only",
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
        news=NewsConfig(
            blackout_minutes_before=15, blackout_minutes_after=15,
            blackout_impacts=["high", "medium"], sources=[], twitter_watchlist=[],
        ),
        alerts=AlertsConfig(), web=WebConfig(),
        database=DatabaseConfig(path=str(db_path), echo=False),
        logging=LoggingConfig(),
    )


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_cal.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────
# parse_feed (pure)
# ──────────────────────────────────────────────────────────────────────


SAMPLE_FEED = json.dumps([
    {
        "title": "Nonfarm Payrolls",
        "country": "USD",
        "date": "2026-05-02T08:30:00-04:00",
        "impact": "High",
        "forecast": "180K",
        "previous": "200K",
    },
    {
        "title": "Bank Holiday",
        "country": "GBP",
        "date": "2026-05-04T00:00:00Z",
        "impact": "Holiday",
    },
    {
        "title": "10y Bond Auction",
        "country": "USD",
        "date": "2026-05-03T13:00:00Z",
        "impact": "Low",
    },
    {
        "title": "Trade Balance",
        "country": "JPY",
        "date": "2026-05-05T23:50:00Z",
        "impact": "Medium",
    },
])


def test_parse_feed_normalizes_impact_and_drops_holidays():
    rows = parse_feed(SAMPLE_FEED)
    # Holiday filtered out
    assert len(rows) == 3
    impacts = {r["name"]: r["impact"] for r in rows}
    assert impacts["Nonfarm Payrolls"] == "high"
    assert impacts["10y Bond Auction"] == "low"
    assert impacts["Trade Balance"] == "medium"


def test_parse_feed_deterministic_source_id():
    rows1 = parse_feed(SAMPLE_FEED)
    rows2 = parse_feed(SAMPLE_FEED)
    assert {r["source_id"] for r in rows1} == {r["source_id"] for r in rows2}


def test_parse_feed_handles_z_and_offset_timestamps():
    rows = parse_feed(SAMPLE_FEED)
    nfp = next(r for r in rows if r["name"] == "Nonfarm Payrolls")
    bond = next(r for r in rows if r["name"] == "10y Bond Auction")
    # NFP: 2026-05-02 08:30:00 EDT = 12:30 UTC
    assert nfp["ts"] == int(datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    # 10y bond: 2026-05-03 13:00 UTC
    assert bond["ts"] == int(datetime(2026, 5, 3, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_parse_feed_rejects_non_json():
    with pytest.raises(ValueError):
        parse_feed("not json")


# ──────────────────────────────────────────────────────────────────────
# Worker refresh + upsert
# ──────────────────────────────────────────────────────────────────────


async def test_refresh_writes_then_updates(temp_db):
    cfg = _cfg(temp_db)
    body_v1 = SAMPLE_FEED

    async def fetcher(_url: str) -> str:
        return body_v1

    w = CalendarWorker(cfg, fetcher=fetcher)
    n = await w._refresh()
    assert n == 3   # holiday filtered

    async with session_factory()() as s:
        rows = (await s.execute(select(CalendarEvent))).scalars().all()
    assert len(rows) == 3

    # Same feed, but NFP now has 'actual' value — should update, not insert.
    body_v2 = json.dumps([
        {
            "title": "Nonfarm Payrolls", "country": "USD",
            "date": "2026-05-02T08:30:00-04:00", "impact": "High",
            "forecast": "180K", "previous": "200K", "actual": "210K",
        },
    ])

    async def fetcher2(_url: str) -> str:
        return body_v2

    w2 = CalendarWorker(cfg, fetcher=fetcher2)
    await w2._refresh()
    async with session_factory()() as s:
        rows = (await s.execute(select(CalendarEvent))).scalars().all()
    assert len(rows) == 3   # still 3 (1 updated + 2 untouched)
    nfp = next(r for r in rows if r.name == "Nonfarm Payrolls")
    assert nfp.actual == "210K"


async def test_has_recent_data_true_when_row_just_fetched(temp_db):
    """The skip-fetch guard should fire when any row was inserted within
    min_refresh_gap_seconds."""
    cfg = _cfg(temp_db)
    from datetime import datetime, timezone
    async with session_factory()() as s:
        s.add(CalendarEvent(
            source="forexfactory", source_id="x1", ts=0,
            name="recent", country="USD", impact="high",
            fetched_at=datetime.now(timezone.utc),
        ))
        await s.commit()
    w = CalendarWorker(cfg, fetcher=lambda u: (_ for _ in ()).throw(Exception("should not fetch")))  # type: ignore[arg-type]
    assert await w._has_recent_data() is True


async def test_has_recent_data_false_when_table_empty(temp_db):
    cfg = _cfg(temp_db)
    w = CalendarWorker(cfg, fetcher=lambda u: (_ for _ in ()).throw(Exception()))  # type: ignore[arg-type]
    assert await w._has_recent_data() is False


# ──────────────────────────────────────────────────────────────────────
# current_blackout helper
# ──────────────────────────────────────────────────────────────────────


async def test_blackout_active_inside_window(temp_db):
    cfg = _cfg(temp_db)
    now_ms = int(datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    # An NFP event happening exactly at now.
    async with session_factory()() as s:
        s.add(CalendarEvent(
            source="forexfactory", source_id="x1", ts=now_ms,
            name="Nonfarm Payrolls", country="USD", impact="high",
        ))
        await s.commit()

    active, name = await current_blackout(now_ms, cfg.news)
    assert active is True
    assert name == "Nonfarm Payrolls"


async def test_blackout_inactive_outside_window(temp_db):
    cfg = _cfg(temp_db)
    base = int(datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    # Event is 60 minutes away — outside the configured 15-min window.
    async with session_factory()() as s:
        s.add(CalendarEvent(
            source="forexfactory", source_id="x1",
            ts=base + 60 * 60_000,
            name="CPI", country="USD", impact="high",
        ))
        await s.commit()
    active, _ = await current_blackout(base, cfg.news)
    assert active is False


async def test_blackout_ignores_impact_not_in_config(temp_db):
    cfg = _cfg(temp_db)   # blackout_impacts = ["high", "medium"]
    now_ms = int(datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    async with session_factory()() as s:
        s.add(CalendarEvent(
            source="forexfactory", source_id="x1", ts=now_ms,
            name="Random low-impact", country="USD", impact="low",
        ))
        await s.commit()
    active, _ = await current_blackout(now_ms, cfg.news)
    assert active is False
