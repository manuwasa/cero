"""
Brain scheduler — the live evaluation loop.

For each configured symbol it subscribes to `candle:closed:{symbol}:{trigger_tf}`
on the bus. When the trigger timeframe's bar closes, it:

  1. Loads all candles per timeframe for that symbol from the DB.
  2. Builds a `MarketContext`.
  3. Runs `evaluate_all` → `aggregate` → `build_signal`.
  4. Persists the signal.
  5. Hands it to the active `ExecutionMode`.

Why 5m as the trigger by default?
  - Frequent enough to react quickly when an LTF setup forms.
  - Sparse enough not to spam: ~12 evaluations per symbol per hour.
  - Aligns with criterion 7's LTF POI lookback.

The brain still uses *all* configured timeframes inside the criterion math —
the trigger just decides *when* to re-evaluate. Pure functions in
criteria.py / scoring.py see the full candle dictionary every time.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import numpy as np
from loguru import logger
from sqlalchemy import desc, select

from cero.brain.criteria import MarketContext, evaluate_all
from cero.brain.indicators import atr
from cero.brain.risk import RiskGate
from cero.brain.scoring import aggregate
from cero.brain.signals import Signal, build_signal, persist_signal
from cero.brain.strategies import ALL_STRATEGIES, Strategy, StrategyContext
from cero.config import Config
from cero.data.calendar_worker import current_blackout
from cero.data.exchange import Candle as CandleType
from cero.data.exchange import ExchangeClient
from cero.db.models import Candle as CandleRow
from cero.db.models import Position as PositionRow
from cero.db.models import Trade as TradeRow
from cero.db.session import session_factory
from cero.events import EventBus, bus as default_bus
from cero.exec.modes import ExecutionMode


# Round-number step per symbol, used by criterion 3 (key_levels) to seed
# candidate horizontal levels around current price. Values sized at roughly
# 0.5% of typical price and snapped to a clean 1/2/5 x 10^N multiple. See
# scripts/suggest_round_step.py to compute these systematically and
# docs/USAGE.md ("Adding a symbol") for how to extend.
_ROUND_STEPS: dict[str, float] = {
    "BTC/USDT:USDT": 500.0,
    "ETH/USDT:USDT": 10.0,
    "SOL/USDT:USDT": 0.5,
    "BNB/USDT:USDT": 2,
}


class BrainScheduler:
    """Owns one task per symbol; each task waits for `candle:closed` events on
    the trigger timeframe and runs the brain pipeline."""

    def __init__(
        self,
        cfg: Config,
        exchange: ExchangeClient,
        risk_gate: RiskGate,
        mode_provider,             # callable returning current ExecutionMode
        *,
        trigger_tf: str = "5m",
        event_bus: Optional[EventBus] = None,
        strategies: Optional[list[Strategy]] = None,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.risk_gate = risk_gate
        self.mode_provider = mode_provider
        self.trigger_tf = trigger_tf
        self.bus = event_bus or default_bus
        # All strategies evaluate on every tick; only signals from the one
        # matching cfg.primary_strategy go to the executor. Others persist
        # as shadow data for comparison.
        self.strategies: list[Strategy] = strategies or list(ALL_STRATEGIES)
        self._tasks: list[asyncio.Task[None]] = []
        self._queues: list[tuple[str, asyncio.Queue]] = []
        self._stop = asyncio.Event()
        self._log = logger.bind(component="scheduler")

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("BrainScheduler already started")
        if self.trigger_tf not in self.cfg.timeframes:
            raise ValueError(
                f"trigger_tf {self.trigger_tf!r} not in config.timeframes "
                f"{self.cfg.timeframes}"
            )
        # Subscribe synchronously so any publish() right after start() is
        # guaranteed to be delivered, even if the per-symbol tasks haven't
        # been scheduled yet.
        for symbol in self.cfg.symbols:
            topic = f"candle:closed:{symbol}:{self.trigger_tf}"
            queue = self.bus.subscribe(topic)
            self._queues.append((topic, queue))
            t = asyncio.create_task(
                self._supervise(symbol, queue),
                name=f"scheduler:{symbol}",
            )
            self._tasks.append(t)
        self._log.info(
            "started ({} symbols, trigger={})", len(self.cfg.symbols), self.trigger_tf,
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
        for topic, queue in self._queues:
            self.bus.unsubscribe(topic, queue)
        self._tasks.clear()
        self._queues.clear()
        self._log.info("stopped")

    # ── per-symbol loop ───────────────────────────────────────────────

    async def _supervise(self, symbol: str, queue: asyncio.Queue) -> None:
        log = self._log.bind(symbol=symbol)
        attempt = 0
        while not self._stop.is_set():
            try:
                msg = await queue.get()
                await self._tick(symbol, msg, log)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(60.0, 2.0**attempt)
                log.exception("tick crashed (attempt {}): {} — sleeping {}s",
                              attempt, e, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _tick(self, symbol: str, _msg, log) -> None:
        """One full brain pass for `symbol`. Runs every registered strategy
        and persists each one's signal. Only signals from `primary_strategy`
        are dispatched to the executor."""
        ctx = await self._build_context(symbol)
        if ctx is None or not ctx.candles:
            log.debug("skip: no candles in DB yet")
            return

        atr_h1 = self._atr_h1(ctx.candles)
        equity, open_positions = await self._account_inputs(symbol)
        today_pnl, consec = await self._trade_history()
        in_blackout, blackout_name = await current_blackout(
            ctx.now_ms, self.cfg.news,
        )

        strat_ctx = StrategyContext(
            market=ctx,
            risk_gate=self.risk_gate,
            equity=equity,
            atr_h1=atr_h1,
            mode=self.cfg.mode,
            open_positions=open_positions,
            today_realized=today_pnl,
            today_consecutive_losses=consec,
            in_blackout=in_blackout,
            blackout_name=blackout_name,
        )

        for strategy in self.strategies:
            try:
                signal = await strategy.evaluate(strat_ctx)
            except Exception as e:  # noqa: BLE001 — one strategy crash mustn't kill others
                log.exception("strategy {} crashed: {}", strategy.name, e)
                continue
            if signal is None:
                continue
            sig_id = await persist_signal(signal)

            # Only the primary strategy's signals reach the executor.
            # Other strategies' signals accumulate as shadow data.
            is_primary = (strategy.name == self.cfg.primary_strategy)
            if is_primary and (signal.is_actionable or signal.tier in ("A", "B", "C")):
                await self.bus.publish(
                    "signal:new",
                    {"signal_id": sig_id, "symbol": symbol, "strategy": strategy.name},
                )
                mode = self.mode_provider()
                await mode.handle_signal(signal)

            log.info(
                "[{}] tier={} dir={} score={} size={:.6f} reason={!r}{}",
                strategy.name, signal.tier, signal.direction, signal.score,
                signal.size, signal.size_reason,
                "" if is_primary else "  (shadow)",
            )

    # ── inputs ────────────────────────────────────────────────────────

    async def _build_context(self, symbol: str) -> Optional[MarketContext]:
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(CandleRow)
                    .where(CandleRow.symbol == symbol)
                    .order_by(CandleRow.timeframe, CandleRow.open_time)
                )
            ).scalars().all()
        if not rows:
            return None
        by_tf: dict[str, list[CandleType]] = {}
        for r in rows:
            by_tf.setdefault(r.timeframe, []).append(
                CandleType(
                    symbol=r.symbol, timeframe=r.timeframe, open_time=r.open_time,
                    open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                )
            )
        # "Now" = latest open_time across all TFs (used by session-of-today logic).
        now_ms = max(c[-1].open_time for c in by_tf.values())
        return MarketContext(
            symbol=symbol, now_ms=now_ms, candles=by_tf,
            weights=self.cfg.criteria_weights,
            round_step=_ROUND_STEPS.get(symbol, 1000.0),
        )

    def _atr_h1(self, candles: dict[str, list[CandleType]]) -> float:
        c1h = candles.get("1h") or []
        if len(c1h) < 15:
            return 0.0
        a = atr(
            [c.high for c in c1h], [c.low for c in c1h], [c.close for c in c1h], 14,
        )
        return float(a[-1]) if not np.isnan(a[-1]) else 0.0

    async def _account_inputs(self, symbol: str) -> tuple[float, int]:
        """Live equity from the exchange + open-position count from the DB.

        Falls back to a conservative default if the exchange call fails so a
        flaky network blip doesn't poison the gate."""
        equity = 0.0
        try:
            bal = await self.exchange.fetch_balance()
            equity = bal.equity
        except Exception as e:  # noqa: BLE001
            self._log.warning("fetch_balance failed in scheduler: {}", e)

        async with session_factory()() as s:
            count = (
                await s.execute(
                    select(PositionRow).where(PositionRow.symbol == symbol)
                )
            ).scalars().all()
        return equity, len(count)

    async def _trade_history(self) -> tuple[float, int]:
        """Realized PnL since UTC midnight + count of trailing losers."""
        now_ms = int(time.time() * 1000)
        day_start = now_ms - (now_ms % 86_400_000)
        async with session_factory()() as s:
            today = (
                await s.execute(
                    select(TradeRow).where(TradeRow.closed_at >= day_start)
                )
            ).scalars().all()
            recent = (
                await s.execute(
                    select(TradeRow).order_by(desc(TradeRow.closed_at)).limit(20)
                )
            ).scalars().all()
        today_pnl = sum(t.realized_pnl for t in today)
        consec = 0
        for t in recent:
            if t.realized_pnl < 0:
                consec += 1
            else:
                break
        return today_pnl, consec
