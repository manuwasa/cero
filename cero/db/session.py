"""
Async engine + session factory.

One process-wide engine; every module that needs DB access gets sessions from
`session_factory()`. SQLite specifics (parent-dir creation, foreign keys,
WAL journal) are handled here so callers don't need to think about them.

Usage:
    from cero.db.session import init_db, session_factory

    await init_db(cfg.database)        # creates dir, opens engine, runs create_all
    async with session_factory()() as s:
        ...
    await close_db()                   # on shutdown
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cero.config import DatabaseConfig
from cero.db.models import Base

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


async def init_db(cfg: DatabaseConfig) -> AsyncEngine:
    """Create the engine, ensure schema exists, return the engine.
    Idempotent — calling it twice is a no-op."""
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine

    # SQLite stores files relative to the cwd; make sure the parent exists.
    Path(cfg.path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        cfg.url,
        echo=cfg.echo,
        future=True,
        pool_pre_ping=True,
    )

    # SQLite pragmas: enforce foreign keys (off by default!) and use WAL so the
    # web dashboard can read concurrently with the workers' writes.
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("db ready: {}", cfg.path)
    return engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory. Caller does `async with f() as s`."""
    if _sessionmaker is None:
        raise RuntimeError("db not initialized — call init_db() first")
    return _sessionmaker


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("db not initialized — call init_db() first")
    return _engine


async def close_db() -> None:
    """Dispose the engine. Call on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        logger.info("db closed")
