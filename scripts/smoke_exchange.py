"""Smoke test for cero/data/exchange.py against the configured exchange.

Reads config.yaml + .env, connects (testnet if configured), and exercises:
  - load markets
  - fetch a ticker
  - fetch 5 candles on 1h
  - fetch balance
  - fetch positions
  - watch_ohlcv for ~10 seconds (proves the WebSocket pipe works)

No orders are placed.
"""
from __future__ import annotations

import asyncio

from cero.config import load_config
from cero.data.exchange import ExchangeClient


async def main() -> None:
    cfg, secrets = load_config()
    print(f"exchange: {cfg.exchange.name}  testnet={cfg.exchange.testnet}")
    print(f"auth:     {'YES' if secrets.exchange_api_key else 'NO'}")

    async with ExchangeClient(cfg, secrets) as ex:
        sym = cfg.symbols[0]

        # 1. Symbol normalization
        norm = ex.normalize_symbol(sym)
        print(f"\n[1] symbol  : {sym} -> {norm}  OK")

        # 2. Ticker (public)
        t = await ex.fetch_ticker(sym)
        print(f"[2] ticker  : last={t.last}  bid={t.bid}  ask={t.ask}  OK")

        # 3. Candles (public)
        candles = await ex.fetch_ohlcv(sym, "1h", limit=5)
        print(f"[3] candles : got {len(candles)} bars on 1h")
        for c in candles[-3:]:
            print(f"             {c.open_time}  O={c.open}  H={c.high}  L={c.low}  C={c.close}")

        if not ex.authenticated:
            print("\n(skipping balance/positions/watch — no API key)")
            return

        # 4. Balance (private)
        bal = await ex.fetch_balance()
        print(f"\n[4] balance : equity={bal.equity} {bal.quote_currency}  "
              f"free={bal.balance}  upnl={bal.unrealized_pnl}  margin_used={bal.margin_used}")

        # 5. Positions (private)
        positions = await ex.fetch_positions(cfg.symbols)
        print(f"[5] positions: {len(positions)} open")
        for p in positions:
            print(f"             {p.symbol} {p.side} size={p.size} entry={p.entry_price} upnl={p.unrealized_pnl}")

        # 6. WebSocket (10s window)
        print("\n[6] watching 1m candles for 10s ...")
        seen = 0

        async def consume():
            nonlocal seen
            async for c in ex.watch_ohlcv(sym, "1m"):
                seen += 1
                print(f"             tick {seen}: {c.open_time} close={c.close}")
                if seen >= 3:
                    return

        try:
            await asyncio.wait_for(consume(), timeout=12)
        except asyncio.TimeoutError:
            print(f"             timed out after 12s (got {seen} ticks — fine on quiet markets)")

    print("\nOK smoke test complete")


if __name__ == "__main__":
    asyncio.run(main())
