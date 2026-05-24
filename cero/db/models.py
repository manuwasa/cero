"""
SQLAlchemy table definitions (2.0 declarative, async-ready).

All timestamps are stored as **Unix milliseconds (int)** — same units ccxt
returns, no timezone ambiguity, comparable with simple integer ops. The brain
and UI convert to datetime only when displaying.

Tables:
    candles          OHLCV per (symbol, timeframe, open_time)
    accounts         equity snapshots over time
    positions        currently-open positions (closed rows are deleted; the
                     historical record lives in `trades`)
    trades           closed trades with realized PnL
    signals          readiness scores emitted by the brain
    news             scraped tweets / headlines
    calendar_events  scheduled economic events

Use `from cero.db.models import Base` and call
`Base.metadata.create_all(engine.sync_engine)` (or async equivalent) once at
boot to materialize tables.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base. Import this anywhere you need metadata."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────
# Market data
# ──────────────────────────────────────────────────────────────────────


class Candle(Base):
    """One OHLCV bar. Composite PK so upserts on (symbol, tf, open_time) are
    cheap and duplicates are impossible.

    `timeframe` is a ccxt-style string: '1m', '5m', '15m', '30m', '1h', '4h', '1d'.
    """

    __tablename__ = "candles"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    close_time: Mapped[int] = mapped_column(BigInteger, nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_candles_symbol_tf_time_desc", "symbol", "timeframe", "open_time"),
    )


# ──────────────────────────────────────────────────────────────────────
# Account / positions / trades
# ──────────────────────────────────────────────────────────────────────


class AccountSnapshot(Base):
    """Account equity at a point in time. account_worker writes a row per poll."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    equity: Mapped[float] = mapped_column(Float, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    margin_used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quote_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")


class Position(Base):
    """A currently-open position, mirrored from the exchange. When the position
    closes, the row is deleted and the historical record is written to `trades`.

    `size` is signed: positive = long, negative = short.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange_position_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # 'long' | 'short'
    size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    mark_price: Mapped[float] = mapped_column(Float, nullable=False)
    leverage: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    opened_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    signal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id"), nullable=True
    )
    signal: Mapped[Optional[Signal]] = relationship(back_populates="positions")


class Trade(Base):
    """A closed position. Realized PnL is final."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)

    opened_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    closed_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'sl' | 'tp' | 'manual' | 'trip' | 'liquidation' | 'other'

    signal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id"), nullable=True
    )
    signal: Mapped[Optional[Signal]] = relationship(back_populates="trades")


# ──────────────────────────────────────────────────────────────────────
# Brain output
# ──────────────────────────────────────────────────────────────────────


class Signal(Base):
    """Brain output: a readiness assessment for one symbol at one point in time.
    The brain writes a row when the score/tier/direction changes meaningfully
    (not on every tick) — see brain/signals.py for the change-detection rules.

    `criteria_json` is the full breakdown of the 8 criteria (passed, weight,
    detail per criterion) so the dashboard can show *why* a tier was assigned
    without re-running the brain.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)  # 'A' | 'B' | 'C' | 'D'
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # 'long' | 'short' | 'none'
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    size_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'signal_only' | 'approval' | 'auto'

    criteria_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    rejected_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    positions: Mapped[list[Position]] = relationship(back_populates="signal")
    trades: Mapped[list[Trade]] = relationship(back_populates="signal")


# ──────────────────────────────────────────────────────────────────────
# News / calendar (context — never auto-triggers a trade)
# ──────────────────────────────────────────────────────────────────────


class TripEvent(Base):
    """Audit log of every TRIP fire. Persisted so the dashboard can show
    history and `/reset` only un-trips the most recent active row. The
    `cleared_at` column is null while the trip is active and gets set when
    the user explicitly resets — never auto-resets (see docs/ARCHITECTURE.md)."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fired_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'manual' | 'daily_loss' | 'consecutive_losses' | 'exchange_errors'
    # | 'unexpected_position' | 'other'
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cleared_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cleared_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 'user' | 'restart' | None


class NewsItem(Base):
    """One scraped headline or tweet. `(source, source_id)` is unique so the
    scraper can dedupe without a separate seen-set."""

    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_news_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CalendarEvent(Base):
    """One scheduled economic event (CPI, FOMC, NFP, ...). Used to blackout
    trading windows around high-impact releases."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_calendar_source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    impact: Mapped[str] = mapped_column(String(8), nullable=False)
    # 'low' | 'medium' | 'high'

    forecast: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    actual: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    previous: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
