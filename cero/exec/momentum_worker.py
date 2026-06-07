"""In-process momentum engine — the daily long/short paper-portfolio worker.

When `config.engine == 'momentum'`, cero/main.py runs THIS instead of the
intraday smc_trend pipeline (price worker + per-symbol scheduler + paper broker).
It wakes every `cfg.momentum.check_hours`, fetches the universe's latest daily
closes via the (mainnet) exchange client, drives the MomentumBook
(mark-to-market + rebalance every `cfg.momentum.rebalance_days`), and pushes a
Telegram notice when it rebalances. Paper only — no real orders, ever.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from cero.brain.momentum import MomentumBook, MomentumConfig
from cero.config import Config
from cero.data.exchange import ExchangeClient
from cero.exec.protocols import Notifier


class MomentumWorker:
    def __init__(self, cfg: Config, exchange: ExchangeClient, notifier: Notifier) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.notifier = notifier
        m = cfg.momentum
        self.mcfg = MomentumConfig(
            universe=tuple(m.universe),
            lookbacks=tuple(m.lookbacks),
            frac=m.frac,
            rebalance_days=m.rebalance_days,
        )
        self.book = MomentumBook(self.mcfg, db_path="data/momentum_paper.db",
                                 start_equity=m.paper_equity)
        self._check_s = max(1, m.check_hours) * 3600
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._log = logger.bind(worker="momentum")

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="momentum")
        self._log.info(
            "momentum engine started — {} coins, rebalance {}d, paper equity {:.0f}",
            len(self.mcfg.universe), self.mcfg.rebalance_days, self.cfg.momentum.paper_equity,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the process
                self._log.exception("momentum cycle crashed: {}", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._check_s)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        closes = await self._fetch_closes()
        if len(closes) < 6:
            self._log.warning("only {} symbols priced — skipping cycle", len(closes))
            return
        summary = self.book.update(closes, int(time.time() * 1000))
        pct = (summary["equity"] / summary["start_equity"] - 1) * 100
        line = (f"[MOM] equity {summary['equity']:.0f} ({pct:+.1f}%) "
                f"day {summary['day_pnl']:+.0f} | "
                f"{len(summary['longs'])}L/{len(summary['shorts'])}S"
                + ("  REBALANCED" if summary["rebalanced"] else ""))
        self._log.info(line)
        if summary["rebalanced"]:
            longs = ", ".join(s.split("/")[0] for s in summary["longs"])
            shorts = ", ".join(s.split("/")[0] for s in summary["shorts"])
            await self.notifier.send_notice(
                f"{line}\nLONG: {longs}\nSHORT: {shorts}"
            )

    async def _fetch_closes(self) -> dict[str, list[float]]:
        need = max(self.mcfg.lookbacks) + 5
        out: dict[str, list[float]] = {}
        for sym in self.mcfg.universe:
            try:
                candles = await self.exchange.fetch_ohlcv(sym, "1d", limit=need)
                if candles:
                    out[sym] = [c.close for c in candles]
            except Exception as e:  # noqa: BLE001 — skip symbols not on the venue
                self._log.debug("fetch {} failed: {}", sym, e)
        return out
