"""Tests for cero/exec/orders.py.

No real exchange — a FakeExchangeClient satisfies just the surface that
CcxtOrderPlacer uses: `create_market_order`, `cancel_all_orders`,
`fetch_positions`, `set_leverage`, `set_margin_mode`, plus the inner ccxt
shim (`._ccxt.markets`, `.amount_to_precision`, `.price_to_precision`,
`.exch_cfg`).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from cero.brain.signals import Signal
from cero.config import DatabaseConfig, ExchangeConfig
from cero.data.exchange import OrderInfo, PositionInfo
from cero.db.models import Position, Signal as SignalRow
from cero.db.session import close_db, init_db, session_factory
from cero.exec.orders import CcxtOrderPlacer, OrderRejectedError


# ──────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────


class FakeCcxt:
    """Stand-in for the inner ccxt instance used by amount/price precision
    helpers and by the markets dict lookup."""

    def __init__(self, market: dict) -> None:
        self.markets = {"ETH/USDT:USDT": market}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        # Round to 3 decimals to mimic a typical perp precision (0.001 ETH).
        return f"{round(float(amount), 3):.3f}"

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{round(float(price), 2):.2f}"


class FakeExchange:
    """Satisfies the subset of ExchangeClient that CcxtOrderPlacer touches."""

    def __init__(self, market: dict | None = None) -> None:
        m = market or {"limits": {"amount": {"min": 0.001}}}
        self._ccxt = FakeCcxt(m)
        self.exch_cfg = ExchangeConfig(
            name="bybit", testnet=True, margin_mode="isolated", leverage=5,
        )
        self.calls_create_market_order: list[dict] = []
        self.calls_cancel: list[str] = []
        self.calls_set_leverage: list[tuple[str, int]] = []
        self.calls_set_margin_mode: list[tuple[str, str]] = []
        self.fetch_positions_result: list[PositionInfo] = []
        self.create_market_order_raises: Exception | None = None

    async def create_market_order(
        self, symbol, side, amount, *, reduce_only=False, params=None
    ):
        self.calls_create_market_order.append({
            "symbol": symbol, "side": side, "amount": amount,
            "reduce_only": reduce_only, "params": params,
        })
        if self.create_market_order_raises is not None:
            raise self.create_market_order_raises
        return OrderInfo(
            id=f"order-{len(self.calls_create_market_order):04d}",
            symbol=symbol, side=side, type="market", amount=amount,
            price=None, filled=amount, status="closed", reduce_only=reduce_only,
        )

    async def cancel_all_orders(self, symbol):
        self.calls_cancel.append(symbol)

    async def fetch_positions(self, symbols=None):
        return list(self.fetch_positions_result)

    async def set_leverage(self, symbol, leverage):
        self.calls_set_leverage.append((symbol, leverage))

    async def set_margin_mode(self, symbol, mode):
        self.calls_set_margin_mode.append((symbol, mode))


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_orders.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


def _signal(**over) -> Signal:
    base = dict(
        ts=1_700_000_000_000,
        symbol="ETH/USDT:USDT", tier="B", direction="long", score=72,
        size_multiplier=0.5, size=0.3125,
        entry_price=3000.0, stop_loss=2920.0, take_profit=3160.0,
        mode="auto", size_reason="ok",
    )
    base.update(over)
    return Signal(**base)


# ──────────────────────────────────────────────────────────────────────
# place() — happy path
# ──────────────────────────────────────────────────────────────────────


async def test_place_happy_path_records_position_and_executes_signal(temp_db):
    ex = FakeExchange()
    # Persist a Signal row so the FK on Position has something to point at.
    async with session_factory()() as s:
        sig_row = SignalRow(
            ts=0, symbol="ETH/USDT:USDT", tier="B", direction="long",
            score=72, size_pct=0.5, mode="auto",
        )
        s.add(sig_row)
        await s.commit()
        await s.refresh(sig_row)
        sig_id = sig_row.id

    placer = CcxtOrderPlacer(ex, signal_id_provider=lambda: sig_id)
    order_id = await placer.place(_signal())

    assert order_id == "order-0001"
    assert len(ex.calls_create_market_order) == 1
    call = ex.calls_create_market_order[0]
    assert call["side"] == "buy"
    assert call["amount"] == 0.312                       # rounded to 3dp
    assert call["params"]["stopLoss"]["triggerPrice"] == 2920.0
    assert call["params"]["takeProfit"]["triggerPrice"] == 3160.0
    # Leverage + margin mode were set
    assert ex.calls_set_leverage == [("ETH/USDT:USDT", 5)]
    assert ex.calls_set_margin_mode == [("ETH/USDT:USDT", "isolated")]
    # Position row written; Signal flagged executed
    async with session_factory()() as s:
        positions = (await s.execute(select(Position))).scalars().all()
        signal = (await s.execute(select(SignalRow))).scalar_one()
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "ETH/USDT:USDT"
    assert p.side == "long"
    assert p.size == pytest.approx(0.312)
    assert p.signal_id == sig_id
    assert signal.executed is True


async def test_place_short_uses_sell_side_and_negative_size(temp_db):
    ex = FakeExchange()
    placer = CcxtOrderPlacer(ex)
    await placer.place(_signal(direction="short"))
    assert ex.calls_create_market_order[0]["side"] == "sell"
    async with session_factory()() as s:
        p = (await s.execute(select(Position))).scalar_one()
    assert p.size < 0


# ──────────────────────────────────────────────────────────────────────
# place() — refusal paths
# ──────────────────────────────────────────────────────────────────────


async def test_place_refuses_non_actionable_signal(temp_db):
    ex = FakeExchange()
    placer = CcxtOrderPlacer(ex)
    result = await placer.place(_signal(size=0.0, size_reason="tier=0"))
    assert result is None
    assert ex.calls_create_market_order == []


async def test_place_refuses_when_size_rounds_to_zero(temp_db):
    ex = FakeExchange()
    placer = CcxtOrderPlacer(ex)
    # 0.0004 < precision of 0.001 → rounds to 0.000
    result = await placer.place(_signal(size=0.0004))
    assert result is None
    assert ex.calls_create_market_order == []


async def test_place_refuses_when_below_exchange_minimum(temp_db):
    # Market minimum is 0.01; our size rounds to 0.005 → below min
    ex = FakeExchange(market={"limits": {"amount": {"min": 0.01}}})
    placer = CcxtOrderPlacer(ex)
    result = await placer.place(_signal(size=0.005))
    assert result is None
    assert ex.calls_create_market_order == []


async def test_place_returns_none_on_exchange_error(temp_db):
    ex = FakeExchange()
    ex.create_market_order_raises = RuntimeError("boom")
    placer = CcxtOrderPlacer(ex)
    result = await placer.place(_signal())
    assert result is None
    # Should NOT record a Position
    async with session_factory()() as s:
        positions = (await s.execute(select(Position))).scalars().all()
    assert positions == []


# ──────────────────────────────────────────────────────────────────────
# cancel_all_for + close_position (TRIP path)
# ──────────────────────────────────────────────────────────────────────


async def test_cancel_all_for_calls_exchange(temp_db):
    ex = FakeExchange()
    placer = CcxtOrderPlacer(ex)
    await placer.cancel_all_for("ETH/USDT:USDT")
    assert ex.calls_cancel == ["ETH/USDT:USDT"]


async def test_close_position_no_op_when_flat(temp_db):
    ex = FakeExchange()
    ex.fetch_positions_result = []   # no open positions
    placer = CcxtOrderPlacer(ex)
    await placer.close_position("ETH/USDT:USDT")
    assert ex.calls_create_market_order == []


async def test_close_position_sells_to_close_a_long(temp_db):
    ex = FakeExchange()
    ex.fetch_positions_result = [
        PositionInfo(
            symbol="ETH/USDT:USDT", side="long", size=0.5,
            entry_price=3000.0, mark_price=3100.0, leverage=5,
        )
    ]
    placer = CcxtOrderPlacer(ex)
    await placer.close_position("ETH/USDT:USDT")
    call = ex.calls_create_market_order[0]
    assert call["side"] == "sell"
    assert call["amount"] == 0.5
    assert call["reduce_only"] is True


async def test_close_position_buys_to_close_a_short(temp_db):
    ex = FakeExchange()
    ex.fetch_positions_result = [
        PositionInfo(
            symbol="ETH/USDT:USDT", side="short", size=-0.5,
            entry_price=3000.0, mark_price=2900.0, leverage=5,
        )
    ]
    placer = CcxtOrderPlacer(ex)
    await placer.close_position("ETH/USDT:USDT")
    call = ex.calls_create_market_order[0]
    assert call["side"] == "buy"
    assert call["amount"] == 0.5
    assert call["reduce_only"] is True
