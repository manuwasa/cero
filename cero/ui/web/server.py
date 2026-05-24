"""
FastAPI dashboard.

Serves a single-page HTML/JS dashboard from `static/`, exposes a small JSON
read API, two POST endpoints for /trip and /reset, and a WebSocket that
mirrors the in-process event bus so the page updates in real time.

By default it binds to 127.0.0.1 (per config.yaml). No auth — localhost only.
If you ever expose this beyond loopback, add token auth + HTTPS first.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from cero.brain.risk import RiskGate
from cero.config import Config
from cero.data.exchange import ExchangeClient
from cero.db.models import (
    Candle as CandleRow,
    NewsItem as NewsRow,
    Position as PositionRow,
    Signal as SignalRow,
    Trade as TradeRow,
    TripEvent,
)
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus


STATIC_DIR = Path(__file__).parent / "static"


def _downsample_snapshots(rows, max_points: int) -> list[dict]:
    """Uniform-stride downsample. Always keeps the first and last row."""
    n = len(rows)
    if n == 0:
        return []
    if n <= max_points:
        keep = rows
    else:
        # Pick max_points indices evenly spaced across [0, n-1].
        stride = (n - 1) / (max_points - 1)
        keep = [rows[round(i * stride)] for i in range(max_points)]
    return [
        {"ts": r.ts, "equity": r.equity, "balance": r.balance,
         "unrealized_pnl": r.unrealized_pnl}
        for r in keep
    ]


# ──────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────


class AccountResp(BaseModel):
    equity: float
    balance: float
    unrealized_pnl: float
    margin_used: float
    quote_currency: str
    source: str   # 'exchange' | 'cached'


class PositionResp(BaseModel):
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    opened_at: int


class ReadinessRow(BaseModel):
    symbol: str
    tier: Optional[str]
    direction: Optional[str]
    score: Optional[int]
    ts: Optional[int]


class PnlResp(BaseModel):
    today_pnl: float
    today_wins: int
    today_losses: int
    today_count: int
    all_time_pnl: float
    all_time_count: int


class TripStatus(BaseModel):
    tripped: bool
    reason: Optional[str]
    detail: str


class CandleResp(BaseModel):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


# ──────────────────────────────────────────────────────────────────────
# WebSocket bridge
# ──────────────────────────────────────────────────────────────────────


class WebSocketBridge:
    """Mirrors selected bus topics to all connected WebSocket clients.

    Each client gets its own asyncio.Queue from the bus; the bridge forwards
    queue items to the client as JSON. A slow/disconnected client doesn't
    block others — each forward task is independent."""

    BRIDGED_TOPICS = ("signal:new", "trip:fired")

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._clients: list[WebSocket] = []
        self._log = logger.bind(component="web.ws")

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.append(websocket)
        queues = {t: self.bus.subscribe(t) for t in self.BRIDGED_TOPICS}
        tasks = [
            asyncio.create_task(self._forward(websocket, topic, q))
            for topic, q in queues.items()
        ]
        try:
            # Hold the connection open until the client disconnects.
            while True:
                # We don't expect client messages, but consume them so the
                # underlying socket stays healthy.
                await websocket.receive_text()
        except WebSocketDisconnect:
            self._log.debug("client disconnected")
        finally:
            for t in tasks:
                t.cancel()
            for topic, q in queues.items():
                self.bus.unsubscribe(topic, q)
            if websocket in self._clients:
                self._clients.remove(websocket)

    async def _forward(self, ws: WebSocket, topic: str, queue: asyncio.Queue) -> None:
        try:
            while True:
                msg = await queue.get()
                payload = {"topic": topic, "data": self._jsonable(msg)}
                try:
                    await ws.send_text(json.dumps(payload))
                except Exception:  # noqa: BLE001 — client gone, give up cleanly
                    return
        except asyncio.CancelledError:
            return

    @staticmethod
    def _jsonable(msg: Any) -> Any:
        if isinstance(msg, (dict, list, str, int, float, bool, type(None))):
            return msg
        if hasattr(msg, "model_dump"):
            return msg.model_dump()
        return str(msg)


# ──────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────


def build_app(
    cfg: Config,
    risk_gate: RiskGate,
    *,
    exchange: Optional[ExchangeClient] = None,
    event_bus: Optional[EventBus] = None,
) -> FastAPI:
    """Construct the FastAPI app. `exchange` is optional — if provided, the
    /api/account endpoint pulls a fresh balance from the exchange; otherwise
    it reads the most recent `accounts` snapshot from the DB.

    Tests use this directly without `exchange` to keep them offline."""

    app = FastAPI(title="Cero", version="0.1.0")
    bridge = WebSocketBridge(event_bus or default_bus)

    # Static dashboard (SPA-ish): one index.html + a couple of assets.
    if STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    @app.get("/")
    async def root():
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            return {"detail": "dashboard not built — see cero/ui/web/static/"}
        return FileResponse(index)

    # ── /api/account ─────────────────────────────────────────────────

    @app.get("/api/account", response_model=AccountResp)
    async def get_account():
        if exchange is not None:
            try:
                bal = await exchange.fetch_balance()
                return AccountResp(
                    equity=bal.equity, balance=bal.balance,
                    unrealized_pnl=bal.unrealized_pnl,
                    margin_used=bal.margin_used,
                    quote_currency=bal.quote_currency,
                    source="exchange",
                )
            except Exception as e:  # noqa: BLE001 — fall back to cache
                logger.warning("account fetch failed, using cache: {}", e)
        # Cached snapshot via the SQLAlchemy AccountSnapshot table.
        from cero.db.models import AccountSnapshot
        async with session_factory()() as s:
            row = (
                await s.execute(
                    select(AccountSnapshot).order_by(desc(AccountSnapshot.ts)).limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return AccountResp(
                equity=0.0, balance=0.0, unrealized_pnl=0.0,
                margin_used=0.0, quote_currency="USDT", source="cached",
            )
        return AccountResp(
            equity=row.equity, balance=row.balance,
            unrealized_pnl=row.unrealized_pnl, margin_used=row.margin_used,
            quote_currency=row.quote_currency, source="cached",
        )

    @app.get("/api/account/history")
    async def get_account_history(hours: int = 24, max_points: int = 200):
        """Return equity snapshots over the last `hours`, downsampled to at
        most `max_points` entries. Downsampling is uniform stride — we keep
        evenly-spaced rows rather than the most recent block, so the chart
        shows the whole window."""
        hours = max(1, min(hours, 24 * 30))         # 1h .. 30 days
        max_points = max(10, min(max_points, 2000))
        from cero.db.models import AccountSnapshot
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - hours * 3600 * 1000
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(AccountSnapshot)
                    .where(AccountSnapshot.ts >= cutoff)
                    .order_by(AccountSnapshot.ts)
                )
            ).scalars().all()
        return _downsample_snapshots(rows, max_points)

    # ── /api/positions ───────────────────────────────────────────────

    @app.get("/api/positions", response_model=list[PositionResp])
    async def get_positions():
        async with session_factory()() as s:
            rows = (
                await s.execute(select(PositionRow).order_by(PositionRow.symbol))
            ).scalars().all()
        return [
            PositionResp(
                symbol=p.symbol, side=p.side, size=p.size,
                entry_price=p.entry_price, mark_price=p.mark_price,
                leverage=p.leverage, unrealized_pnl=p.unrealized_pnl,
                stop_loss=p.stop_loss, take_profit=p.take_profit,
                opened_at=p.opened_at,
            )
            for p in rows
        ]

    # ── /api/readiness ───────────────────────────────────────────────

    @app.get("/api/readiness", response_model=list[ReadinessRow])
    async def get_readiness():
        async with session_factory()() as s:
            # Latest signal per symbol — we pull recent rows and dedupe in
            # Python rather than fight SQLite's lack of DISTINCT ON.
            rows = (
                await s.execute(
                    select(SignalRow).order_by(desc(SignalRow.ts)).limit(50)
                )
            ).scalars().all()
        latest: dict[str, SignalRow] = {}
        for r in rows:
            if r.symbol not in latest:
                latest[r.symbol] = r

        out: list[ReadinessRow] = []
        for sym in cfg.symbols:
            r = latest.get(sym)
            if r is None:
                out.append(ReadinessRow(symbol=sym, tier=None, direction=None, score=None, ts=None))
            else:
                out.append(ReadinessRow(
                    symbol=sym, tier=r.tier, direction=r.direction,
                    score=r.score, ts=r.ts,
                ))
        return out

    @app.get("/api/readiness/{symbol:path}")
    async def get_readiness_one(symbol: str):
        async with session_factory()() as s:
            r = (
                await s.execute(
                    select(SignalRow)
                    .where(SignalRow.symbol == symbol)
                    .order_by(desc(SignalRow.ts))
                    .limit(1)
                )
            ).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, f"no signal for {symbol}")
        return {
            "symbol": r.symbol, "tier": r.tier, "direction": r.direction,
            "score": r.score, "ts": r.ts, "criteria": json.loads(r.criteria_json or "[]"),
        }

    # ── /api/pnl ─────────────────────────────────────────────────────

    @app.get("/api/pnl", response_model=PnlResp)
    async def get_pnl():
        now_ms = int(time.time() * 1000)
        day_start = now_ms - (now_ms % 86_400_000)
        async with session_factory()() as s:
            today = (
                await s.execute(
                    select(TradeRow).where(TradeRow.closed_at >= day_start)
                )
            ).scalars().all()
            all_total = (
                await s.execute(
                    select(func.coalesce(func.sum(TradeRow.realized_pnl), 0.0))
                )
            ).scalar_one()
            all_count = (
                await s.execute(select(func.count()).select_from(TradeRow))
            ).scalar_one()
        return PnlResp(
            today_pnl=sum(t.realized_pnl for t in today),
            today_wins=sum(1 for t in today if t.realized_pnl > 0),
            today_losses=sum(1 for t in today if t.realized_pnl < 0),
            today_count=len(today),
            all_time_pnl=float(all_total),
            all_time_count=int(all_count),
        )

    # ── /api/candles/{symbol}/{tf} ───────────────────────────────────

    @app.get("/api/candles/{symbol:path}/{tf}")
    async def get_candles(symbol: str, tf: str, limit: int = 200):
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(CandleRow)
                    .where(CandleRow.symbol == symbol)
                    .where(CandleRow.timeframe == tf)
                    .order_by(desc(CandleRow.open_time))
                    .limit(max(1, min(limit, 1000)))
                )
            ).scalars().all()
        # Reverse so oldest is first (chart-friendly).
        return [
            CandleResp(
                open_time=r.open_time, open=r.open, high=r.high,
                low=r.low, close=r.close, volume=r.volume,
            )
            for r in reversed(rows)
        ]

    # ── /api/trip and /api/reset ─────────────────────────────────────

    @app.get("/api/trip", response_model=TripStatus)
    async def get_trip_status():
        return TripStatus(
            tripped=risk_gate.tripped,
            reason=risk_gate.trip_reason,
            detail=risk_gate.trip_detail,
        )

    @app.post("/api/trip", response_model=TripStatus)
    async def post_trip(payload: dict | None = None):
        detail = (payload or {}).get("detail", "via dashboard")
        if not risk_gate.tripped:
            await risk_gate.trip("manual", detail)
        return TripStatus(
            tripped=risk_gate.tripped,
            reason=risk_gate.trip_reason,
            detail=risk_gate.trip_detail,
        )

    @app.post("/api/reset", response_model=TripStatus)
    async def post_reset():
        await risk_gate.reset(by="dashboard")
        return TripStatus(
            tripped=risk_gate.tripped,
            reason=risk_gate.trip_reason,
            detail=risk_gate.trip_detail,
        )

    @app.get("/api/news")
    async def get_news(limit: int = 20):
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(NewsRow)
                    .order_by(desc(NewsRow.ts))
                    .limit(max(1, min(limit, 100)))
                )
            ).scalars().all()
        return [
            {
                "ts": r.ts, "source": r.source, "author": r.author,
                "content": r.content, "url": r.url,
            }
            for r in rows
        ]

    @app.get("/api/trips")
    async def get_trip_history():
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(TripEvent).order_by(desc(TripEvent.fired_at)).limit(20)
                )
            ).scalars().all()
        return [
            {
                "fired_at": t.fired_at, "reason": t.reason,
                "detail": t.detail, "cleared_at": t.cleared_at,
                "cleared_by": t.cleared_by,
            }
            for t in rows
        ]

    # ── /api/status ──────────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status():
        return {
            "exchange": cfg.exchange.name,
            "testnet": cfg.exchange.testnet,
            "mode": cfg.mode,
            "symbols": cfg.symbols,
            "timeframes": cfg.timeframes,
            "tripped": risk_gate.tripped,
        }

    # ── WebSocket ────────────────────────────────────────────────────

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        await bridge.handle(websocket)

    return app
