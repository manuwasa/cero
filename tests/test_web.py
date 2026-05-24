"""Tests for cero/ui/web/server.py.

Uses FastAPI's httpx-based TestClient. No exchange — exchange=None forces the
/api/account endpoint into its cached-snapshot fallback path. We seed the DB
with rows and assert the JSON shape and the trip/reset round-trip.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cero.brain.risk import RiskGate
from cero.config import (
    AlertsConfig, Config, CriteriaWeights, DatabaseConfig, ExchangeConfig,
    LoggingConfig, NewsConfig, RiskConfig, WebConfig,
)
from cero.db.models import AccountSnapshot, Position, Signal, Trade
from cero.db.session import close_db, init_db, session_factory
from cero.events import EventBus
from cero.ui.web.server import build_app


def _cfg(db_path: Path) -> Config:
    return Config(
        exchange=ExchangeConfig(name="bybit", testnet=True),
        symbols=["ETH/USDT:USDT"],
        timeframes=["5m", "1h"],
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
async def client():
    tmp = Path(tempfile.gettempdir()) / "cero_test_web.db"
    tmp.unlink(missing_ok=True)
    cfg = _cfg(tmp)
    await init_db(cfg.database)

    bus = EventBus()
    gate = RiskGate(cfg.risk, cfg.news, event_bus=bus)

    app = build_app(cfg, gate, exchange=None, event_bus=bus)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, gate

    await close_db()
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────
# Smoke / read endpoints
# ──────────────────────────────────────────────────────────────────────


async def test_root_serves_index(client):
    c, _ = client
    r = await c.get("/")
    assert r.status_code == 200
    body = r.text.lower()
    assert "cero" in body
    assert "<html" in body


async def test_status_reports_config(client):
    c, _ = client
    r = await c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["exchange"] == "bybit"
    assert body["testnet"] is True
    assert body["symbols"] == ["ETH/USDT:USDT"]
    assert body["tripped"] is False


async def test_account_falls_back_to_cached_snapshot(client):
    c, _ = client
    # No snapshots yet — should still respond with zeroed cached values.
    r = await c.get("/api/account")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "cached"
    assert body["equity"] == 0.0

    # Seed a snapshot and ensure it surfaces.
    async with session_factory()() as s:
        s.add(AccountSnapshot(
            ts=1_700_000_000_000, equity=12_345.67, balance=12_300.0,
            unrealized_pnl=45.67, margin_used=10.0, quote_currency="USDT",
        ))
        await s.commit()
    r = await c.get("/api/account")
    body = r.json()
    assert body["equity"] == pytest.approx(12_345.67)
    assert body["unrealized_pnl"] == pytest.approx(45.67)


async def test_positions_empty_then_populated(client):
    c, _ = client
    r = await c.get("/api/positions")
    assert r.status_code == 200
    assert r.json() == []

    async with session_factory()() as s:
        s.add(Position(
            symbol="ETH/USDT:USDT", side="long", size=0.5,
            entry_price=3000.0, mark_price=3050.0, leverage=5,
            stop_loss=2920.0, take_profit=3160.0,
            opened_at=1_700_000_000_000, updated_at=1_700_000_000_000,
        ))
        await s.commit()
    r = await c.get("/api/positions")
    rows = r.json()
    assert len(rows) == 1
    p = rows[0]
    assert p["symbol"] == "ETH/USDT:USDT"
    assert p["side"] == "long"
    assert p["stop_loss"] == 2920.0


async def test_readiness_returns_all_symbols_even_when_none_signaled(client):
    c, _ = client
    r = await c.get("/api/readiness")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETH/USDT:USDT"
    assert rows[0]["tier"] is None


async def test_readiness_returns_latest_per_symbol(client):
    c, _ = client
    async with session_factory()() as s:
        # Two signals for ETH at different times — latest should win
        s.add(Signal(
            ts=1_000, symbol="ETH/USDT:USDT", tier="C", direction="long",
            score=45, size_pct=0.0, mode="signal_only",
        ))
        s.add(Signal(
            ts=2_000, symbol="ETH/USDT:USDT", tier="B", direction="long",
            score=72, size_pct=0.5, mode="signal_only",
        ))
        await s.commit()
    r = await c.get("/api/readiness")
    row = r.json()[0]
    assert row["tier"] == "B"
    assert row["score"] == 72


async def test_pnl_today_split(client):
    c, _ = client
    # day_start = midnight UTC; we use a far-future timestamp to guarantee "today".
    import time
    now_ms = int(time.time() * 1000)
    day_start = now_ms - (now_ms % 86_400_000)
    async with session_factory()() as s:
        for pnl in (50.0, -20.0, 30.0):
            s.add(Trade(
                symbol="ETH/USDT:USDT", side="long", size=0.5,
                entry_price=3000.0, exit_price=3050.0,
                opened_at=day_start, closed_at=day_start + 100,
                realized_pnl=pnl, exit_reason="manual",
            ))
        # one yesterday — should not count today
        s.add(Trade(
            symbol="ETH/USDT:USDT", side="long", size=0.5,
            entry_price=3000.0, exit_price=2900.0,
            opened_at=day_start - 86_400_000, closed_at=day_start - 1_000,
            realized_pnl=-50.0, exit_reason="manual",
        ))
        await s.commit()
    r = await c.get("/api/pnl")
    body = r.json()
    assert body["today_pnl"] == pytest.approx(60.0)
    assert body["today_wins"] == 2
    assert body["today_losses"] == 1
    assert body["today_count"] == 3
    assert body["all_time_count"] == 4


# ──────────────────────────────────────────────────────────────────────
# Trip / reset round-trip
# ──────────────────────────────────────────────────────────────────────


async def test_trip_status_then_reset_round_trip(client):
    c, gate = client
    r = await c.get("/api/trip")
    assert r.json()["tripped"] is False

    r = await c.post("/api/trip", json={"detail": "manual via test"})
    assert r.status_code == 200
    assert r.json()["tripped"] is True
    assert gate.tripped is True
    assert "manual via test" in gate.trip_detail

    r = await c.post("/api/reset")
    assert r.status_code == 200
    assert r.json()["tripped"] is False
    assert gate.tripped is False


async def test_account_history_empty_initially(client):
    c, _ = client
    r = await c.get("/api/account/history")
    assert r.status_code == 200
    assert r.json() == []


async def test_account_history_returns_snapshots_in_order(client):
    c, _ = client
    import time as _time
    now_ms = int(_time.time() * 1000)
    async with session_factory()() as s:
        # Three snapshots, latest first
        for offset_min, eq in ((-30, 10_000), (-20, 10_050), (-10, 10_100)):
            s.add(AccountSnapshot(
                ts=now_ms + offset_min * 60_000,
                equity=eq, balance=eq, unrealized_pnl=0.0,
                margin_used=0.0, quote_currency="USDT",
            ))
        await s.commit()
    r = await c.get("/api/account/history?hours=1")
    rows = r.json()
    assert len(rows) == 3
    # Order ascending by ts
    assert rows[0]["equity"] == 10_000
    assert rows[-1]["equity"] == 10_100


async def test_account_history_downsamples_when_over_max_points(client):
    c, _ = client
    import time as _time
    now_ms = int(_time.time() * 1000)
    async with session_factory()() as s:
        # 500 evenly-spaced snapshots over the last hour
        for i in range(500):
            s.add(AccountSnapshot(
                ts=now_ms - (500 - i) * 6_000,
                equity=10_000 + i,
                balance=10_000 + i, unrealized_pnl=0.0,
                margin_used=0.0, quote_currency="USDT",
            ))
        await s.commit()
    r = await c.get("/api/account/history?hours=24&max_points=50")
    rows = r.json()
    assert len(rows) == 50
    # Endpoints preserved
    assert rows[0]["equity"] == 10_000
    assert rows[-1]["equity"] == 10_499


async def test_account_history_filters_by_window(client):
    c, _ = client
    import time as _time
    now_ms = int(_time.time() * 1000)
    async with session_factory()() as s:
        s.add(AccountSnapshot(   # old — should be excluded
            ts=now_ms - 48 * 3600_000,
            equity=9_000, balance=9_000, unrealized_pnl=0.0,
            margin_used=0.0, quote_currency="USDT",
        ))
        s.add(AccountSnapshot(   # recent
            ts=now_ms - 10 * 60_000,
            equity=10_000, balance=10_000, unrealized_pnl=0.0,
            margin_used=0.0, quote_currency="USDT",
        ))
        await s.commit()
    r = await c.get("/api/account/history?hours=1")
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["equity"] == 10_000


async def test_double_trip_is_idempotent_via_api(client):
    c, gate = client
    await c.post("/api/trip", json={"detail": "first"})
    await c.post("/api/trip", json={"detail": "second"})  # ignored — already tripped
    assert gate.trip_detail == "first"  # first one stuck


# ──────────────────────────────────────────────────────────────────────
# HTTP Basic Auth
# ──────────────────────────────────────────────────────────────────────


async def test_no_auth_when_credentials_empty(client):
    """The default fixture has no auth_user/auth_pass — every endpoint
    should be reachable without Authorization headers."""
    c, _ = client
    r = await c.get("/api/status")
    assert r.status_code == 200


async def test_auth_enforced_when_configured():
    """With auth_user + auth_pass set, requests without Basic auth → 401."""
    import base64, tempfile
    from cero.brain.risk import RiskGate
    from cero.events import EventBus
    tmp = Path(tempfile.gettempdir()) / "cero_test_web_auth.db"
    tmp.unlink(missing_ok=True)
    cfg = _cfg(tmp)
    cfg.web.auth_user = "alice"
    cfg.web.auth_pass = "secret"
    await init_db(cfg.database)
    try:
        gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
        app = build_app(cfg, gate, exchange=None, event_bus=EventBus())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # No auth header → 401
            r = await c.get("/api/status")
            assert r.status_code == 401
            assert "WWW-Authenticate" in {k.title(): v for k, v in r.headers.items()} or \
                   "www-authenticate" in r.headers
            # Wrong creds → 401
            wrong = base64.b64encode(b"alice:wrongpass").decode()
            r = await c.get("/api/status", headers={"Authorization": f"Basic {wrong}"})
            assert r.status_code == 401
            # Right creds → 200
            right = base64.b64encode(b"alice:secret").decode()
            r = await c.get("/api/status", headers={"Authorization": f"Basic {right}"})
            assert r.status_code == 200
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


async def test_auth_misconfigured_raises_at_build():
    """Setting only one of user/pass is a config error — fail loudly."""
    import tempfile, pytest as _pytest
    from cero.brain.risk import RiskGate
    from cero.events import EventBus
    tmp = Path(tempfile.gettempdir()) / "cero_test_web_misauth.db"
    tmp.unlink(missing_ok=True)
    cfg = _cfg(tmp)
    cfg.web.auth_user = "alice"
    cfg.web.auth_pass = ""    # password missing → half-configured
    await init_db(cfg.database)
    try:
        gate = RiskGate(cfg.risk, cfg.news, event_bus=EventBus())
        with _pytest.raises(ValueError):
            build_app(cfg, gate, exchange=None)
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
