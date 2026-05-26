"""Wipe signals + trades + trip events to start a fresh validation period.

Keeps: candles (slow to re-backfill), account snapshots, news, calendar.
Wipes: signals, trades, positions, trip events.

Use this when you've fundamentally changed the strategy/scoring logic and
existing data no longer reflects what the system would emit going forward.

Usage:
    uv run python scripts/reset_validation_data.py              # dry run
    uv run python scripts/reset_validation_data.py --commit     # actually wipe
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete, func, select

from cero.config import load_config
from cero.db.models import Position, Signal, Trade, TripEvent
from cero.db.session import close_db, init_db, session_factory


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit", action="store_true",
        help="actually delete; default is dry-run preview",
    )
    args = parser.parse_args()

    cfg, _ = load_config()
    await init_db(cfg.database)

    async with session_factory()() as s:
        n_signals = (await s.execute(select(func.count()).select_from(Signal))).scalar_one()
        n_trades = (await s.execute(select(func.count()).select_from(Trade))).scalar_one()
        n_positions = (await s.execute(select(func.count()).select_from(Position))).scalars().all()
        n_trips = (await s.execute(select(func.count()).select_from(TripEvent))).scalar_one()

    print(f"=== Reset preview ===")
    print(f"  signals to delete:    {n_signals}")
    print(f"  trades to delete:     {n_trades}")
    print(f"  positions to delete:  {len(n_positions)}")
    print(f"  trip events to clear: {n_trips}")
    print()

    if not args.commit:
        print("DRY RUN — nothing deleted. Pass --commit to actually wipe.")
        await close_db()
        return

    print("This will permanently delete the data above.")
    confirm = input("Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("aborted.")
        await close_db()
        return

    async with session_factory()() as s:
        # Order matters: trades + positions reference signals via FK.
        await s.execute(delete(Trade))
        await s.execute(delete(Position))
        await s.execute(delete(Signal))
        await s.execute(delete(TripEvent))
        await s.commit()

    print(f"wiped: {n_signals} signals, {n_trades} trades, "
          f"{len(n_positions)} positions, {n_trips} trip events")
    print("candles, account snapshots, news, and calendar preserved.")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
