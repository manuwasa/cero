"""In-process momentum engine — the daily long/short paper-portfolio worker.

When `config.engine == 'momentum'`, cero/main.py runs THIS instead of the
intraday smc_trend pipeline. Each cycle (every `cfg.momentum.check_hours`) it:
  - (if auto_universe) refreshes the universe = the top-N most-liquid perps,
  - fetches daily closes for the universe PLUS anything currently held,
  - drives the MomentumBook (mark-to-market + rebalance every rebalance_days),
  - pushes a Telegram notice on rebalance.
Paper only — no real orders, ever.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from loguru import logger

from cero.brain.momentum import MomentumBook, MomentumConfig, read_book
from cero.config import Config
from cero.data.exchange import ExchangeClient
from cero.exec.protocols import Notifier

if TYPE_CHECKING:
    from cero.brain.risk import RiskGate


class MomentumWorker:
    def __init__(self, cfg: Config, exchange: ExchangeClient, notifier: Notifier,
                 risk_gate: "Optional[RiskGate]" = None) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.notifier = notifier
        self.risk_gate = risk_gate
        m = cfg.momentum
        self.auto = m.auto_universe
        self.mcfg = MomentumConfig(
            universe=tuple(m.universe),          # fallback / used when auto is off
            lookbacks=tuple(m.lookbacks),
            frac=m.frac,
            rebalance_days=m.rebalance_days,
            gross_per_side=m.gross_per_side,
            weighting=m.weighting,
            vol_window=m.vol_window,
            target_vol=m.target_vol,
            max_gross_per_side=m.max_gross_per_side,
            daily_loss_halt_pct=m.daily_loss_halt_pct,
            drawdown_halt_pct=m.drawdown_halt_pct,
        )
        self.book = MomentumBook(self.mcfg, db_path="data/momentum_paper.db",
                                 start_equity=m.paper_equity)
        self._check_s = max(1, m.check_hours) * 3600
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._log = logger.bind(worker="momentum")

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="momentum")
        uni = "auto (top-{} liquid)".format(self.cfg.momentum.universe_size) if self.auto \
            else f"fixed {len(self.mcfg.universe)} coins"
        self._log.info("momentum engine started — universe: {}, rebalance {}d, equity {:.0f}",
                       uni, self.mcfg.rebalance_days, self.cfg.momentum.paper_equity)

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
        # 1. (auto) refresh the universe to the current most-liquid perps
        if self.auto:
            try:
                uni = await self.exchange.top_liquid_perps(
                    self.cfg.momentum.universe_size, self.cfg.momentum.min_volume_usd)
                if len(uni) >= 6:
                    self.mcfg = replace(self.mcfg, universe=tuple(uni))  # keep risk overlay
                    self._log.info("auto-universe: {} liquid perps", len(uni))
                else:
                    self._log.warning("auto-universe returned {} — keeping previous", len(uni))
            except Exception as e:  # noqa: BLE001
                self._log.warning("universe refresh failed, keeping previous: {}", e)

        # 2. fetch closes for the universe + anything we still hold (so dropped
        #    coins can be marked + closed cleanly)
        held = list(read_book(self.book.db_path).get("positions", {}))
        symbols = list(dict.fromkeys(list(self.mcfg.universe) + held))
        closes = await self._fetch_closes(symbols)
        if len(closes) < 6:
            self._log.warning("only {} symbols priced — skipping cycle", len(closes))
            return

        # 3. drive the book: mark to market, apply the risk gate (kill switch +
        #    circuit breaker → flatten to cash), rebalance only when due & healthy.
        external_halt = bool(self.risk_gate and self.risk_gate.tripped)
        summary = self.book.update(closes, int(time.time() * 1000), external_halt=external_halt)
        pct = (summary["equity"] / summary["start_equity"] - 1) * 100
        tag = ("  FLATTENED" if summary["flattened"]
               else "  REBALANCED" if summary["rebalanced"]
               else "  HALTED" if summary["halt_reason"] else "")
        line = (f"[MOM] equity {summary['equity']:.0f} ({pct:+.1f}%) "
                f"day {summary['day_pnl']:+.0f} | "
                f"{len(summary['longs'])}L/{len(summary['shorts'])}S{tag}")
        self._log.info(line)

        reason = summary["halt_reason"]
        if reason and not external_halt:
            # circuit-breaker breach → trip the gate so it STAYS halted until the
            # user /reset's, then shout. This is the loss-containment kill.
            self._log.warning("[MOM] circuit breaker tripped: {}", reason)
            if self.risk_gate is not None:
                try:
                    await self.risk_gate.trip("circuit_breaker", reason)
                except Exception as e:  # noqa: BLE001
                    self._log.warning("could not trip risk gate: {}", e)
            await self.notifier.send_notice(
                f"🛑 CIRCUIT BREAKER — {reason}\n"
                f"equity {summary['equity']:.0f} ({pct:+.1f}%). Book flattened to cash, "
                f"trading halted. Send /reset to resume.")
        elif summary["flattened"] and external_halt:
            await self.notifier.send_notice(
                f"🛑 kill switch active — momentum book flattened to cash "
                f"({summary['equity']:.0f}). Send /reset to resume.")
        elif summary["rebalanced"]:
            longs = ", ".join(s.split("/")[0] for s in summary["longs"])
            shorts = ", ".join(s.split("/")[0] for s in summary["shorts"])
            await self.notifier.send_notice(f"{line}\nLONG: {longs}\nSHORT: {shorts}")

    async def _fetch_closes(self, symbols) -> dict[str, list[float]]:
        need = max(self.mcfg.lookbacks) + 5
        out: dict[str, list[float]] = {}
        for sym in symbols:
            try:
                candles = await self.exchange.fetch_ohlcv(sym, "1d", limit=need)
                if candles:
                    out[sym] = [c.close for c in candles]
            except Exception as e:  # noqa: BLE001 — skip symbols not on the venue
                self._log.debug("fetch {} failed: {}", sym, e)
        return out
