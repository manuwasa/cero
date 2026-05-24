"""
Price worker.

For each (symbol, timeframe) pair in config it runs one asyncio task that:
  1. Backfills `cfg.backfill_candles` historical bars via REST on startup.
  2. Then watches the live WebSocket stream and upserts every update.
  3. Publishes events on the `bus`:
       "candle:{symbol}:{tf}"        — fired on every update (in-progress)
       "candle:closed:{symbol}:{tf}" — fired once when a bar closes
     The brain subscribes to the "closed" topic so it only re-evaluates on
     fully-formed bars.

Each task is independently supervised with exponential backoff so a single
WebSocket hiccup can't take down the rest of the system.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cero.config import Config
from cero.data.exchange import Candle, ExchangeClient
from cero.db.models import Candle as CandleRow
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus


class PriceWorker:
    """Owns one task per (symbol, timeframe). Start once, await stop() to drain."""

    def __init__(
        self,
        cfg: Config,
        exchange: ExchangeClient,
        *,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.bus = event_bus or default_bus
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._log = logger.bind(worker="price")

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn one supervised task per (symbol, timeframe)."""
        if self._tasks:
            raise RuntimeError("PriceWorker already started")
        for symbol in self.cfg.symbols:
            for tf in self.cfg.timeframes:
                t = asyncio.create_task(
                    self._supervise(symbol, tf),
                    name=f"price:{symbol}:{tf}",
                )
                self._tasks.append(t)
        self._log.info(
            "started {} streams ({} symbols x {} timeframes)",
            len(self._tasks), len(self.cfg.symbols), len(self.cfg.timeframes),
        )

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._log.info("stopped")

    # ── per-stream loop with backoff ──────────────────────────────────

    async def _supervise(self, symbol: str, tf: str) -> None:
        """Restart the stream on transient errors with exponential backoff."""
        log = self._log.bind(symbol=symbol, tf=tf)
        attempt = 0
        while not self._stop.is_set():
            try:
                if attempt == 0:
                    await self._backfill(symbol, tf, log)
                await self._watch(symbol, tf, log)
                attempt = 0  # _watch only returns on cancel or _stop
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(60.0, 2.0**attempt)
                log.exception(
                    "stream crashed (attempt {}): {} — restarting in {}s",
                    attempt, e, delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    # ── backfill ──────────────────────────────────────────────────────

    async def _backfill(self, symbol: str, tf: str, log) -> None:
        n = self.cfg.backfill_candles
        if n <= 0:
            return
        candles = await self.exchange.fetch_ohlcv(symbol, tf, limit=n)
        if not candles:
            log.warning("backfill returned no candles")
            return
        await self._upsert_many(candles)
        log.info(
            "backfilled {} bars (latest open_time={})",
            len(candles), candles[-1].open_time,
        )

    # ── live watch ────────────────────────────────────────────────────

    async def _watch(self, symbol: str, tf: str, log) -> None:
        """Loop forever, yielding live candle updates from the WebSocket."""
        last_open_time: Optional[int] = None
        last_candle: Optional[Candle] = None
        async for c in self.exchange.watch_ohlcv(symbol, tf):
            if self._stop.is_set():
                return

            # When open_time advances, the previous bar has just closed —
            # persist it as final and fire the "closed" event before moving on.
            if last_open_time is not None and c.open_time > last_open_time:
                if last_candle is not None:
                    await self._upsert_one(last_candle)
                    await self.bus.publish(
                        f"candle:closed:{symbol}:{tf}", last_candle
                    )
                    log.debug("bar closed: open_time={}", last_candle.open_time)

            await self._upsert_one(c)
            await self.bus.publish(f"candle:{symbol}:{tf}", c)
            last_open_time = c.open_time
            last_candle = c

    # ── upsert helpers ────────────────────────────────────────────────

    async def _upsert_one(self, c: Candle) -> None:
        await self._upsert_many([c])

    async def _upsert_many(self, candles: list[Candle]) -> None:
        if not candles:
            return
        rows = [
            {
                "symbol": c.symbol,
                "timeframe": c.timeframe,
                "open_time": c.open_time,
                "close_time": c.close_time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        stmt = sqlite_insert(CandleRow).values(rows)
        # ON CONFLICT(symbol, timeframe, open_time) DO UPDATE — overwrite the
        # OHLCV fields. PK columns aren't in set_ since they define the conflict.
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "open_time"],
            set_={
                "close_time": stmt.excluded.close_time,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        async with session_factory()() as s:
            await s.execute(stmt)
            await s.commit()
