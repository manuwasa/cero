"""Backfill REAL (mainnet) historical candles into a separate cache DB.

Why this exists: Cero runs on Bybit *testnet*, whose OHLCV is garbage (SOL froze
at 1372, spiked to 9900 on 2026-05-29/30, while real SOL was ~$80). Every backtest
run against the live DB is therefore meaningless. This script pulls *mainnet*
candles — public data, NO API keys, NO trading — into `data/cero_mainnet.db` so
scripts/backtest_mainnet.py can replay the strategies over real prices.

Uses synchronous ccxt (requests-based) to dodge the aiodns/Windows DNS issue the
live async client works around.

Usage:
    uv run python scripts/backfill_mainnet.py
    uv run python scripts/backfill_mainnet.py --days 45
    uv run python scripts/backfill_mainnet.py --symbols BTC/USDT:USDT,SOL/USDT:USDT
"""
from __future__ import annotations

import argparse
import sqlite3
import time

import ccxt

TF_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
DEFAULT_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
]
DEFAULT_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            close_time INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            PRIMARY KEY (symbol, timeframe, open_time)
        )"""
    )
    con.commit()


def fetch_paginated(ex, symbol: str, tf: str, since: int, now: int) -> list:
    """Page forward from `since` to `now`, returning all OHLCV rows."""
    step = TF_MS[tf]
    out: list = []
    cursor = since
    while cursor < now:
        try:
            batch = ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
        except Exception as e:  # noqa: BLE001
            print(f"      ! fetch error at {cursor}: {repr(e)[:120]} — retrying once")
            time.sleep(1.0)
            batch = ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
        if not batch:
            break
        out.extend(batch)
        last = batch[-1][0]
        if last <= cursor:        # no forward progress → done
            break
        cursor = last + step
        if len(batch) < 1000:     # exchange gave us everything up to now
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_mainnet.db")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TFS))
    ap.add_argument("--exchange", default="bybit")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    ex = getattr(ccxt, args.exchange)({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "swap"},   # MAINNET — no set_sandbox_mode
    })
    now = ex.milliseconds()
    start = now - args.days * 86_400_000

    con = sqlite3.connect(args.db)
    ensure_schema(con)

    print(f"=== backfill {args.exchange} MAINNET -- {args.days}d -> {args.db} ===")
    grand = 0
    for symbol in symbols:
        for tf in tfs:
            try:
                rows = fetch_paginated(ex, symbol, tf, start, now)
                payload = [
                    (symbol, tf, int(r[0]), int(r[0]) + TF_MS[tf] - 1,
                     float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                    for r in rows if r[0] >= start
                ]
                con.executemany(
                    "INSERT OR REPLACE INTO candles "
                    "(symbol,timeframe,open_time,close_time,open,high,low,close,volume) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", payload,
                )
                con.commit()
                grand += len(payload)
                print(f"  {symbol:<18} {tf:<4} {len(payload):>6} bars")
            except Exception as e:  # noqa: BLE001 — skip symbols not on this exchange
                print(f"  {symbol:<18} {tf:<4} SKIP ({repr(e)[:50]})")
    con.close()
    print(f"done. {grand} bars total -> {args.db}")


if __name__ == "__main__":
    main()
