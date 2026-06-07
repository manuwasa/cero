"""Long/short momentum — PAPER portfolio trader (daily batch).

Runs the cero.brain.momentum engine as a real, stateful paper book. Designed to
be run ONCE PER DAY (cron / a daily call from your start script) — momentum is a
daily strategy, so it doesn't need an always-on streamer.

Each run:
  1. fetch the universe's latest daily closes (mainnet, public, no keys),
  2. mark the held book to market (update paper equity by the day's P&L),
  3. if a rebalance is due (every cfg.rebalance_days), recompute the target
     long/short book and trade the *difference* (net of cost),
  4. persist state + positions + a trade log to data/momentum_paper.db,
  5. print the book + equity.

NO real money — this is the forward paper-validation of the momentum edge before
any live trading. Expect it to be volatile and to underperform the backtest
(survivorship bias + parameter sensitivity — see cero/brain/momentum.py).

Usage:
    uv run python scripts/momentum_paper.py            # run today's update
    uv run python scripts/momentum_paper.py --status   # just show the book, no trade
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timezone

import ccxt

from cero.brain.momentum import MomentumConfig, momentum_score, target_weights

UNIVERSE = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT", "LTC/USDT:USDT",
    "DOT/USDT:USDT", "ATOM/USDT:USDT", "NEAR/USDT:USDT", "APT/USDT:USDT", "ARB/USDT:USDT",
    "OP/USDT:USDT", "SUI/USDT:USDT", "TON/USDT:USDT", "TRX/USDT:USDT", "FIL/USDT:USDT",
    "ETC/USDT:USDT", "INJ/USDT:USDT", "SEI/USDT:USDT", "TIA/USDT:USDT", "RUNE/USDT:USDT",
    "AAVE/USDT:USDT", "UNI/USDT:USDT", "GALA/USDT:USDT", "SAND/USDT:USDT", "AXS/USDT:USDT",
    "GRT/USDT:USDT", "ALGO/USDT:USDT", "CRV/USDT:USDT", "LDO/USDT:USDT", "DYDX/USDT:USDT",
    "1000PEPE/USDT:USDT", "WIF/USDT:USDT", "WLD/USDT:USDT", "STX/USDT:USDT", "IMX/USDT:USDT",
    "HBAR/USDT:USDT", "ENA/USDT:USDT", "ORDI/USDT:USDT",
]
START_EQUITY = 10_000.0
COST = 0.001            # one-way, fraction of notional traded
DB = "data/momentum_paper.db"
DAY_MS = 86_400_000


def fetch_closes(ex, symbols, lookback_days):
    out: dict[str, list[float]] = {}
    since = ex.milliseconds() - (lookback_days + 5) * DAY_MS
    for s in symbols:
        try:
            ohlcv = ex.fetch_ohlcv(s, "1d", since=since, limit=lookback_days + 5)
            if ohlcv:
                out[s] = [c[4] for c in ohlcv]
        except Exception:  # noqa: BLE001 — skip symbols not on the venue
            pass
    return out


def ensure_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS mom_state (id INTEGER PRIMARY KEY, equity REAL, last_rebalance INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS mom_positions (symbol TEXT PRIMARY KEY, size REAL, last_price REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS mom_trades (ts INTEGER, symbol TEXT, side TEXT, qty REAL, price REAL, cost REAL)")
    con.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show book only, no rebalance/trade")
    args = ap.parse_args()

    cfg = MomentumConfig(universe=tuple(UNIVERSE))
    ex = ccxt.bybit({"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "swap"}})
    closes = fetch_closes(ex, UNIVERSE, max(cfg.lookbacks) + 5)
    prices = {s: c[-1] for s, c in closes.items() if c}
    now = ex.milliseconds()

    con = sqlite3.connect(DB)
    ensure_db(con)
    st = con.execute("SELECT equity, last_rebalance FROM mom_state WHERE id=1").fetchone()
    equity, last_reb = st if st else (START_EQUITY, 0)
    positions = {s: (sz, lp) for s, sz, lp in con.execute("SELECT symbol, size, last_price FROM mom_positions")}

    # 1. mark to market: add the P&L since each position's last recorded price
    day_pnl = sum(sz * (prices[s] - lp) for s, (sz, lp) in positions.items() if s in prices)
    equity += day_pnl
    positions = {s: (sz, prices.get(s, lp)) for s, (sz, lp) in positions.items()}

    due = (now - last_reb) >= cfg.rebalance_days * DAY_MS
    rebalanced = False
    if due and not args.status:
        w = target_weights(closes, cfg)
        if w:
            target = {s: w[s] * equity / prices[s] for s in w if s in prices}  # signed coin sizes
            traded_cost = 0.0
            syms = set(positions) | set(target)
            new_positions = {}
            for s in syms:
                cur = positions.get(s, (0.0, prices.get(s, 0.0)))[0]
                tgt = target.get(s, 0.0)
                if abs(tgt - cur) > 1e-12 and s in prices:
                    qty = tgt - cur
                    c = abs(qty) * prices[s] * COST
                    traded_cost += c
                    con.execute("INSERT INTO mom_trades VALUES (?,?,?,?,?,?)",
                                (now, s, "buy" if qty > 0 else "sell", qty, prices[s], c))
                if abs(tgt) > 1e-12:
                    new_positions[s] = (tgt, prices[s])
            equity -= traded_cost
            positions = new_positions
            last_reb = now
            rebalanced = True

    # persist
    if not args.status:
        con.execute("INSERT OR REPLACE INTO mom_state (id, equity, last_rebalance) VALUES (1,?,?)", (equity, last_reb))
        con.execute("DELETE FROM mom_positions")
        con.executemany("INSERT INTO mom_positions VALUES (?,?,?)",
                        [(s, sz, lp) for s, (sz, lp) in positions.items()])
        con.commit()
    con.close()

    # report
    score = momentum_score(closes, cfg.lookbacks)
    longs = sorted((s for s, (sz, _) in positions.items() if sz > 0), key=lambda s: -score.get(s, 0))
    shorts = sorted((s for s, (sz, _) in positions.items() if sz < 0), key=lambda s: score.get(s, 0))
    nextd = max(0, cfg.rebalance_days - int((now - last_reb) / DAY_MS))
    print(f"\n=== Momentum paper book — {datetime.fromtimestamp(now/1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC ===")
    print(f"universe priced: {len(prices)}/{len(UNIVERSE)}   day P&L: {day_pnl:+.2f}")
    print(f"paper equity: {equity:.2f}  ({(equity/START_EQUITY-1)*100:+.2f}% since start)")
    print(f"{'REBALANCED' if rebalanced else ('status only' if args.status else 'no rebalance')}; next rebalance in ~{nextd}d\n")
    print(f"LONG  ({len(longs)}): " + ", ".join(s.split('/')[0] for s in longs))
    print(f"SHORT ({len(shorts)}): " + ", ".join(s.split('/')[0] for s in shorts))


if __name__ == "__main__":
    main()
