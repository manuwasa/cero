"""End-to-end smoke for the brain:
  1. Spin up the price worker for ~25s to backfill + a few live ticks.
  2. Load all candles from the DB into a MarketContext per symbol.
  3. Run evaluate_all on each symbol.
  4. Print pass/fail per criterion.

This proves the whole pipeline: exchange -> worker -> DB -> brain.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import select

from cero.brain.criteria import MarketContext, evaluate_all
from cero.brain.scoring import aggregate
from cero.config import load_config
from cero.data.exchange import Candle as CandleType
from cero.data.exchange import ExchangeClient
from cero.data.price_worker import PriceWorker
from cero.db.models import Candle as CandleRow
from cero.db.session import close_db, init_db, session_factory


ROUND_STEPS = {
    "BTC/USDT:USDT": 1000.0,
    "ETH/USDT:USDT": 100.0,
    "SOL/USDT:USDT": 10.0,
}


async def load_context(symbol: str, weights, round_step: float, now_ms: int) -> MarketContext:
    """Pull every candle for `symbol` out of the DB and group by timeframe."""
    async with session_factory()() as s:
        rows = (
            await s.execute(
                select(CandleRow)
                .where(CandleRow.symbol == symbol)
                .order_by(CandleRow.timeframe, CandleRow.open_time)
            )
        ).scalars().all()
    by_tf: dict[str, list[CandleType]] = {}
    for r in rows:
        by_tf.setdefault(r.timeframe, []).append(
            CandleType(
                symbol=r.symbol, timeframe=r.timeframe, open_time=r.open_time,
                open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
            )
        )
    return MarketContext(
        symbol=symbol, now_ms=now_ms, candles=by_tf,
        weights=weights, round_step=round_step,
    )


async def main() -> None:
    cfg, secrets = load_config()
    tmp = Path(tempfile.gettempdir()) / "cero_smoke_brain.db"
    if tmp.exists():
        tmp.unlink()
    cfg.database.path = str(tmp)
    await init_db(cfg.database)

    print(f"symbols: {cfg.symbols}")
    print(f"timeframes: {cfg.timeframes}")

    async with ExchangeClient(cfg, secrets) as ex:
        worker = PriceWorker(cfg, ex)
        worker.start()
        print("\nwarming up data (25s) ...")
        await asyncio.sleep(25)
        await worker.stop()

    now_ms = int(asyncio.get_event_loop().time() * 0)  # placeholder
    # Use the latest candle's open_time as "now" so session-of-today logic
    # uses the right UTC date regardless of when this script is run.
    async with session_factory()() as s:
        latest = (
            await s.execute(select(CandleRow.open_time).order_by(CandleRow.open_time.desc()).limit(1))
        ).scalar_one_or_none()
    if latest is None:
        print("no candles in DB after worker run — aborting")
        return
    now_ms = latest

    for symbol in cfg.symbols:
        ctx = await load_context(symbol, cfg.criteria_weights, ROUND_STEPS.get(symbol, 1000.0), now_ms)
        results = evaluate_all(ctx)
        report = aggregate(results, cfg.risk)
        actionable = "ACTIONABLE" if report.is_actionable else "no trade"
        print(
            f"\n=== {symbol}  price={ctx.current_price:.2f}  "
            f"score={report.score}/100  tier={report.tier}  "
            f"direction={report.direction}  size={report.size_multiplier}x  [{actionable}] ==="
        )
        for r in results:
            mark = "PASS" if r.passed else "fail"
            dh = f"  -> {r.direction_hint}" if r.direction_hint else ""
            print(f"  [{mark}] {r.name:20s} ({r.weight:2d}) {r.detail}{dh}")

    await close_db()
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    print("\nOK brain smoke complete")


if __name__ == "__main__":
    asyncio.run(main())
