"""End-to-end smoke covering brain -> signal -> mode dispatch.

Backfills real bybit testnet candles, runs the brain per symbol, builds a
Signal, then runs the actionable ones through all three execution modes
(with a stub placer / log notifier) so we can see what each mode would do.

Also fires a TRIP to verify the TripWatcher cancels orders + closes positions.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import numpy as np
from sqlalchemy import select

from cero.brain.criteria import MarketContext, evaluate_all
from cero.brain.indicators import atr as atr_indicator
from cero.brain.risk import RiskGate
from cero.brain.scoring import aggregate
from cero.brain.signals import Signal, build_signal, persist_signal
from cero.config import load_config
from cero.data.exchange import Candle as CandleType
from cero.data.exchange import ExchangeClient
from cero.data.price_worker import PriceWorker
from cero.db.models import Candle as CandleRow
from cero.db.session import close_db, init_db, session_factory
from cero.exec.modes import (
    LogNotifier,
    StubOrderPlacer,
    TripWatcher,
    build_mode,
)


ROUND_STEPS = {
    "BTC/USDT:USDT": 1000.0,
    "ETH/USDT:USDT": 100.0,
    "SOL/USDT:USDT": 10.0,
}


async def load_context(symbol: str, weights, round_step: float, now_ms: int) -> MarketContext:
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


def atr_h1(candles: dict[str, list[CandleType]]) -> float:
    c1h = candles.get("1h") or []
    if len(c1h) < 15:
        return 0.0
    a = atr_indicator(
        [c.high for c in c1h], [c.low for c in c1h], [c.close for c in c1h], 14,
    )
    return float(a[-1]) if not np.isnan(a[-1]) else 0.0


async def main() -> None:
    cfg, secrets = load_config()
    tmp = Path(tempfile.gettempdir()) / "cero_smoke_modes.db"
    tmp.unlink(missing_ok=True)
    cfg.database.path = str(tmp)
    await init_db(cfg.database)

    notifier = LogNotifier()
    placer = StubOrderPlacer()
    risk_gate = RiskGate(cfg.risk, cfg.news)
    await risk_gate.hydrate()

    # Wire the trip watcher so we can verify it later.
    watcher = TripWatcher(notifier, placer, cfg.symbols)
    watcher.start()

    async with ExchangeClient(cfg, secrets) as ex:
        worker = PriceWorker(cfg, ex)
        worker.start()
        print(f"\nwarming up data (~25s) — mode in config.yaml is '{cfg.mode}'")
        await asyncio.sleep(25)
        await worker.stop()

        # Use the latest candle's open_time as 'now' so the brain's session
        # logic operates on the right UTC date.
        async with session_factory()() as s:
            now_ms = (
                await s.execute(select(CandleRow.open_time).order_by(CandleRow.open_time.desc()).limit(1))
            ).scalar_one()

        equity = 10_000.0   # testnet starting balance assumption

        for symbol in cfg.symbols:
            ctx = await load_context(symbol, cfg.criteria_weights, ROUND_STEPS.get(symbol, 1000.0), now_ms)
            report = aggregate(evaluate_all(ctx), cfg.risk)
            atr1 = atr_h1(ctx.candles)
            signal = build_signal(
                ctx=ctx, report=report, risk_gate=risk_gate,
                equity=equity, atr_h1=atr1, mode=cfg.mode,
            )
            sig_id = await persist_signal(signal)
            print(
                f"\n=== {symbol} ===\n"
                f"  score={signal.score}  tier={signal.tier}  dir={signal.direction}  "
                f"entry={signal.entry_price:.2f}  sl={signal.stop_loss:.2f}  tp={signal.take_profit:.2f}\n"
                f"  size={signal.size:.6f}  reason={signal.size_reason!r}  "
                f"actionable={signal.is_actionable}  db_id={sig_id}"
            )

            print("  --- handling under each mode ---")
            for mode_name in ("signal_only", "approval", "auto"):
                mode = build_mode(
                    mode_name, notifier=notifier, placer=placer, risk_gate=risk_gate,
                    approval_timeout_s=1.0,
                )
                print(f"  [{mode_name}]")
                await mode.handle_signal(signal)

        # Now fire a manual trip to prove the trip watcher reacts.
        print("\n=== firing manual TRIP ===")
        await risk_gate.trip("manual", "smoke test")
        await asyncio.sleep(0.2)
        print(f"placer.canceled: {placer.canceled}")
        print(f"placer.closed:   {placer.closed}")

    await watcher.stop()
    await close_db()
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    print("\nOK modes smoke complete")


if __name__ == "__main__":
    asyncio.run(main())
