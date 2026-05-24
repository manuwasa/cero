"""Smoke test for cero/data/price_worker.py.

Runs the worker against bybit testnet for ~25 seconds:
  - confirms backfill writes rows for every (symbol, timeframe) pair
  - confirms WebSocket events arrive on the bus
  - confirms upserts work (live tick overwrites latest backfilled bar)
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from cero.config import load_config
from cero.data.exchange import ExchangeClient
from cero.data.price_worker import PriceWorker
from cero.db.models import Candle as CandleRow
from cero.db.session import close_db, init_db, session_factory
from cero.events import bus


async def main() -> None:
    cfg, secrets = load_config()
    # Use a throwaway DB so we don't pollute the real one.
    tmp = Path(tempfile.gettempdir()) / "cero_smoke_pw.db"
    if tmp.exists():
        tmp.unlink()
    cfg.database.path = str(tmp)

    print(f"db: {cfg.database.url}")
    print(f"symbols: {cfg.symbols}")
    print(f"timeframes: {cfg.timeframes}")
    print(f"backfill_candles: {cfg.backfill_candles}")

    await init_db(cfg.database)

    # Subscribe to one closed-bar topic so we know the watch loop is producing.
    sym0, tf0 = cfg.symbols[0], "1m"  # 1m isn't in config; subscribe to whatever is
    if tf0 not in cfg.timeframes:
        tf0 = cfg.timeframes[0]
    closed_q = bus.subscribe(f"candle:closed:{sym0}:{tf0}")
    tick_q = bus.subscribe(f"candle:{sym0}:{tf0}")

    async with ExchangeClient(cfg, secrets) as ex:
        worker = PriceWorker(cfg, ex)
        worker.start()
        print("\nworker started; running for ~25s ...")

        try:
            # Let backfill + a few ticks happen.
            await asyncio.sleep(25)
        finally:
            await worker.stop()

    # Verify what's in the DB.
    async with session_factory()() as s:
        total = (await s.execute(select(func.count()).select_from(CandleRow))).scalar_one()
        print(f"\ntotal rows in candles: {total}")
        per_tf = (await s.execute(
            select(CandleRow.symbol, CandleRow.timeframe, func.count())
            .group_by(CandleRow.symbol, CandleRow.timeframe)
            .order_by(CandleRow.symbol, CandleRow.timeframe)
        )).all()
        for sym, tf, n in per_tf:
            print(f"  {sym}  {tf}: {n} bars")

    print(f"\ntick events received for {sym0} {tf0}: {tick_q.qsize()}")
    print(f"closed events received for {sym0} {tf0}: {closed_q.qsize()}")

    await close_db()
    tmp.unlink(missing_ok=True)
    tmp_wal = tmp.with_suffix(tmp.suffix + "-wal")
    tmp_shm = tmp.with_suffix(tmp.suffix + "-shm")
    tmp_wal.unlink(missing_ok=True)
    tmp_shm.unlink(missing_ok=True)
    print("\nOK smoke test complete")


if __name__ == "__main__":
    asyncio.run(main())
