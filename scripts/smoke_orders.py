"""End-to-end smoke for cero/exec/orders.py — places a REAL order on testnet.

[!]This script places a live market order on bybit testnet. No real money,
   but real exchange state. Do not run it on mainnet without rewriting.

What it does:
  1. Connect to bybit testnet.
  2. Fetch the current ETH/USDT:USDT price and your balance.
  3. Build a Signal with a tiny 0.01 ETH long, brackets at ~0.5% wide (the
     production brain uses ATR; we use percentages here so a quiet testnet
     market doesn't trigger SL the instant the position opens).
  4. CcxtOrderPlacer.place() — submits the entry with native SL/TP brackets.
  5. Wait 10s, fetch positions, print what the exchange reports.
  6. close_position() to flatten — exercises the TRIP path.
  7. Verify position is gone.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from cero.brain.signals import Signal
from cero.config import load_config
from cero.data.exchange import ExchangeClient
from cero.db.session import close_db, init_db
from cero.exec.orders import CcxtOrderPlacer


SYMBOL = "ETH/USDT:USDT"
SIZE = 0.01           # tiny — ~$30 notional at $3000
BRACKET_PCT = 0.005   # 0.5% wide brackets


async def main() -> None:
    cfg, secrets = load_config()
    tmp = Path(tempfile.gettempdir()) / "cero_smoke_orders.db"
    tmp.unlink(missing_ok=True)
    cfg.database.path = str(tmp)
    await init_db(cfg.database)

    async with ExchangeClient(cfg, secrets) as ex:
        # 1. Sanity: balance + price
        bal = await ex.fetch_balance()
        print(f"balance: equity={bal.equity:.2f} {bal.quote_currency}  free={bal.balance:.2f}")
        if bal.equity <= 0:
            print("[!]testnet balance is 0 — claim funds at testnet.bybit.com first")
            return

        ticker = await ex.fetch_ticker(SYMBOL)
        price = ticker.last
        sl = price * (1 - BRACKET_PCT)
        tp = price * (1 + BRACKET_PCT)
        print(f"price: {price:.2f}   sl: {sl:.2f}   tp: {tp:.2f}")

        # 2. Build a Signal that looks like the production brain would emit
        sig = Signal(
            ts=int(time.time() * 1000),
            symbol=SYMBOL, tier="A", direction="long", score=85,
            size_multiplier=1.0, size=SIZE,
            entry_price=price, stop_loss=sl, take_profit=tp,
            mode="auto", size_reason="smoke test",
        )

        placer = CcxtOrderPlacer(ex)

        # 3. Place — this is the live order
        print("\n>>> placing entry...")
        order_id = await placer.place(sig)
        print(f"order_id: {order_id}")

        if order_id is None:
            print("[!]place returned None — see logs above")
            return

        # 4. Wait + verify position is open on the exchange
        print("\n>>> waiting 10s, then checking exchange position state...")
        await asyncio.sleep(10)
        positions = await ex.fetch_positions([SYMBOL])
        if not positions:
            print("[!]no position reported by exchange. Maybe insta-filled SL/TP?")
        else:
            for p in positions:
                print(f"  {p.symbol}  {p.side}  size={p.size}  entry={p.entry_price}  "
                      f"upnl={p.unrealized_pnl}  sl={p.stop_loss}  tp={p.take_profit}")

        # 5. Close — exercises the TRIP path (reduce-only opposite-side)
        print("\n>>> close_position (TRIP path)...")
        await placer.close_position(SYMBOL)
        await asyncio.sleep(3)
        post_close = await ex.fetch_positions([SYMBOL])
        if not post_close:
            print("OK position closed cleanly")
        else:
            for p in post_close:
                if p.size != 0:
                    print(f"[!]position still open: {p.side} size={p.size}")
                else:
                    print(f"  {p.symbol}: flat")

        # 6. Cancel any stragglers (SL/TP that didn't auto-cancel on close)
        await placer.cancel_all_for(SYMBOL)

    await close_db()
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    print("\nOK orders smoke complete")


if __name__ == "__main__":
    asyncio.run(main())
