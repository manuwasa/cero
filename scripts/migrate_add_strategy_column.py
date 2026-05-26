"""Migration: add `strategy` column to the signals table.

Cero now supports running multiple strategies in parallel for A/B testing.
Each signal is tagged with which strategy emitted it. Old rows get NULL,
backfilled to 'smc_trend' (the original strategy) so backtest filtering works.

Idempotent: safe to run multiple times. Detects whether the column exists
and only ALTERs if missing.

Usage:
    uv run python scripts/migrate_add_strategy_column.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from cero.config import load_config
from cero.db.session import close_db, get_engine, init_db


async def main() -> None:
    cfg, _ = load_config()
    await init_db(cfg.database)
    engine = get_engine()
    async with engine.begin() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(signals)"))).fetchall()
        existing = {c[1] for c in cols}
        if "strategy" in existing:
            print("strategy column already exists — nothing to do")
        else:
            await conn.execute(text("ALTER TABLE signals ADD COLUMN strategy TEXT"))
            print("added strategy column to signals")

        # Backfill NULLs with 'smc_trend' so existing signals are attributable
        result = await conn.execute(
            text("UPDATE signals SET strategy = 'smc_trend' WHERE strategy IS NULL")
        )
        print(f"backfilled {result.rowcount} rows with strategy='smc_trend'")

        # Add the index if missing
        idxs = (await conn.execute(text("PRAGMA index_list(signals)"))).fetchall()
        idx_names = {row[1] for row in idxs}
        if "ix_signals_strategy" not in idx_names:
            await conn.execute(text("CREATE INDEX ix_signals_strategy ON signals(strategy)"))
            print("created index ix_signals_strategy")

    await close_db()
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
