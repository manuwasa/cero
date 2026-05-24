"""
Order placement.

The real `OrderPlacer` — submits the market entry with native SL/TP brackets,
records the Position in the DB, and flips the Signal's `executed` flag. This
is the only module in Cero that places live orders.

Design choices:
  - Bybit (and most modern perp exchanges) support **position-level** SL/TP
    attached to the entry order. ccxt's unified `params={"stopLoss": ...,
    "takeProfit": ...}` handles this — no manual OCO needed because the
    exchange treats them as one bracket. cero/exec/oco.py stays empty until
    we need a manual OCO fallback for an exchange that lacks native support.
  - Lot-size + min-amount rounding is done via the loaded market metadata.
    If the computed size rounds to zero (e.g. tier-B sizing with a tiny stop),
    we refuse to place rather than place an unintended dust order.
  - Every exchange call is wrapped — a failure during entry refuses to place;
    a failure during the *record* step is logged but doesn't reverse the live
    order (we never want to silently cancel a position the user thinks is
    open). Reconciliation belongs to account_worker, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy import update

from cero.brain.signals import Signal
from cero.data.exchange import ExchangeClient, OrderInfo
from cero.db.models import Position, Signal as SignalRow
from cero.db.session import session_factory


class OrderRejectedError(Exception):
    """Raised when a precondition check fails before we hit the exchange.
    `place()` catches and logs these so the caller sees a clean None return."""


@dataclass
class _Bracket:
    side: str         # 'buy' | 'sell'  (the entry side, ccxt convention)
    amount: float     # already precision-rounded
    sl: float
    tp: float


class CcxtOrderPlacer:
    """Real `OrderPlacer` that talks to ccxt via the ExchangeClient."""

    def __init__(
        self,
        exchange: ExchangeClient,
        *,
        signal_id_provider=None,
    ) -> None:
        """
        `signal_id_provider`: optional callable that returns the latest persisted
        Signal row id, so we can write the FK on the Position row. The brain
        passes one when wiring this together; the smoke test can omit it.
        """
        self.exchange = exchange
        self._signal_id_provider = signal_id_provider
        self._log = logger.bind(component="orders")

    # ── OrderPlacer protocol ──────────────────────────────────────────

    async def place(self, signal: Signal) -> Optional[str]:
        log = self._log.bind(symbol=signal.symbol, signal_ts=signal.ts)
        if not signal.is_actionable:
            log.info("refuse: not actionable ({})", signal.size_reason)
            return None

        try:
            bracket = self._prepare(signal)
        except OrderRejectedError as e:
            log.warning("rejected before exchange: {}", e)
            return None

        # 1) Make sure leverage + margin mode match what the user configured.
        #    These calls are idempotent on bybit (set_leverage swallows the
        #    "not modified" error inside the exchange wrapper).
        try:
            await self.exchange.set_leverage(
                signal.symbol, self.exchange.exch_cfg.leverage
            )
            await self.exchange.set_margin_mode(
                signal.symbol, self.exchange.exch_cfg.margin_mode
            )
        except Exception as e:  # noqa: BLE001
            log.warning("leverage/margin setup failed: {}", e)

        # 2) Submit market entry with native bracket params.
        params = {
            "stopLoss": {
                "triggerPrice": self._price(signal.symbol, bracket.sl),
                "type": "market",
            },
            "takeProfit": {
                "triggerPrice": self._price(signal.symbol, bracket.tp),
                "type": "market",
            },
        }
        try:
            order: OrderInfo = await self.exchange.create_market_order(
                signal.symbol, bracket.side, bracket.amount, params=params
            )
        except Exception as e:  # noqa: BLE001
            log.exception("entry order failed: {}", e)
            return None

        log.info(
            "ENTRY {} {} {} @ market id={}  sl={} tp={}",
            signal.symbol, bracket.side, bracket.amount, order.id,
            bracket.sl, bracket.tp,
        )

        # 2b) Fetch the actual filled amount. Bybit (and most exchanges) treat
        #     market orders as IOC by default — if liquidity is thin (testnet
        #     books, low-cap alts, off-hours), the order partial-fills and the
        #     unfilled portion is canceled. We need the real fill size for the
        #     Position row, otherwise our records overstate the actual exposure.
        actual_amount = bracket.amount
        try:
            fetched = await self.exchange.fetch_order(order.id, signal.symbol)
            if fetched.filled > 0:
                actual_amount = fetched.filled
            if fetched.filled < bracket.amount * 0.99:    # >1% short = partial
                log.warning(
                    "partial fill on {}: requested {} got {} (status={})",
                    signal.symbol, bracket.amount, fetched.filled, fetched.status,
                )
            if fetched.filled == 0:
                log.warning(
                    "order {} returned with filled=0 (status={}) — not recording",
                    order.id, fetched.status,
                )
                return order.id
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fetch_order failed for {}, using requested amount: {}",
                order.id, e,
            )

        # 3) Record the Position with the *actual* filled amount. Failure here
        #    doesn't reverse the live order — account_worker reconciles state
        #    from the exchange on the next poll.
        try:
            bracket_actual = _Bracket(
                side=bracket.side, amount=actual_amount,
                sl=bracket.sl, tp=bracket.tp,
            )
            await self._record(signal, order, bracket_actual)
        except Exception as e:  # noqa: BLE001
            log.exception("position record failed (order id={}): {}", order.id, e)

        return order.id

    async def cancel_all_for(self, symbol: str) -> None:
        try:
            await self.exchange.cancel_all_orders(symbol)
            self._log.info("canceled all orders on {}", symbol)
        except Exception as e:  # noqa: BLE001
            self._log.exception("cancel_all_orders({}) failed: {}", symbol, e)

    async def close_position(self, symbol: str) -> None:
        try:
            positions = await self.exchange.fetch_positions([symbol])
        except Exception as e:  # noqa: BLE001
            self._log.exception("fetch_positions({}) failed: {}", symbol, e)
            return
        for p in positions:
            if p.size == 0:
                continue
            # Reduce-only market order in the opposite direction.
            exit_side: str = "sell" if p.size > 0 else "buy"
            amount = abs(p.size)
            try:
                await self.exchange.create_market_order(
                    symbol, exit_side, amount, reduce_only=True
                )
                self._log.info(
                    "CLOSE {} {} {} (reduce-only)", symbol, exit_side, amount
                )
            except Exception as e:  # noqa: BLE001
                self._log.exception("close_position({}) failed: {}", symbol, e)

    # ── internals ─────────────────────────────────────────────────────

    def _prepare(self, signal: Signal) -> _Bracket:
        """Apply exchange precision + min-size guards. Raises
        OrderRejectedError on any disqualification."""
        if signal.direction not in ("long", "short"):
            raise OrderRejectedError(f"bad direction {signal.direction!r}")

        market = self._market(signal.symbol)
        # Round size down to the exchange's amount precision.
        amount = float(
            self.exchange._ccxt.amount_to_precision(signal.symbol, signal.size)
        )
        if amount <= 0:
            raise OrderRejectedError(
                f"size {signal.size} rounds to 0 at exchange precision"
            )
        min_amount = float(
            ((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0
        )
        if min_amount > 0 and amount < min_amount:
            raise OrderRejectedError(
                f"amount {amount} < exchange minimum {min_amount}"
            )

        ccxt_side = "buy" if signal.direction == "long" else "sell"
        return _Bracket(
            side=ccxt_side, amount=amount,
            sl=signal.stop_loss, tp=signal.take_profit,
        )

    def _market(self, symbol: str) -> dict:
        markets = getattr(self.exchange._ccxt, "markets", None) or {}
        market = markets.get(symbol)
        if market is None:
            raise OrderRejectedError(f"market metadata missing for {symbol}")
        return market

    def _price(self, symbol: str, price: float) -> float:
        """Round a price to the exchange's tick size."""
        return float(self.exchange._ccxt.price_to_precision(symbol, price))

    async def _record(
        self, signal: Signal, order: OrderInfo, bracket: _Bracket
    ) -> None:
        """Insert a Position row and flag the Signal as executed.

        We look up the matching Signal row by (symbol, ts) rather than
        requiring it to be passed in. The scheduler always persists the
        Signal before calling mode.handle_signal, so it's there in the DB
        by the time we run.
        """
        from sqlalchemy import desc, select   # local — used only here

        size_signed = bracket.amount if signal.direction == "long" else -bracket.amount

        async with session_factory()() as s:
            # Find the matching Signal row written by the scheduler.
            sig_row = (
                await s.execute(
                    select(SignalRow)
                    .where(SignalRow.symbol == signal.symbol)
                    .where(SignalRow.ts == signal.ts)
                    .order_by(desc(SignalRow.id)).limit(1)
                )
            ).scalar_one_or_none()
            signal_id = sig_row.id if sig_row else None

            # exchange_position_id stays None. The order id is NOT the
            # position id on bybit (fetch_positions returns None there); if
            # we stored it here, account_worker's symbol+side fallback key
            # wouldn't match on the next reconcile and would TRIP on our own
            # freshly-placed trade. The signal_id below is the real link.
            row = Position(
                exchange_position_id=None,
                symbol=signal.symbol,
                side=signal.direction,
                size=size_signed,
                entry_price=signal.entry_price,
                mark_price=signal.entry_price,
                leverage=float(self.exchange.exch_cfg.leverage),
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                opened_at=signal.ts,
                updated_at=signal.ts,
                signal_id=signal_id,
            )
            s.add(row)
            if signal_id is not None:
                await s.execute(
                    update(SignalRow)
                    .where(SignalRow.id == signal_id)
                    .values(executed=True)
                )
            await s.commit()
