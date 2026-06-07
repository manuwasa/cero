"""Long/short momentum — standalone PAPER trader (daily batch).

Thin wrapper around cero.brain.momentum.MomentumBook (the SAME engine the
in-process momentum worker uses, so there's one implementation). Run once a day.
This is handy for manual runs / inspection; the normal way to run momentum is
just `python -m cero` with `engine: momentum` in config.yaml.

Usage:
    uv run python scripts/momentum_paper.py            # run today's update
    uv run python scripts/momentum_paper.py --status   # show the book, no trade
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import ccxt

from cero.brain.momentum import MomentumBook, MomentumConfig
from cero.config import DEFAULT_MOMENTUM_UNIVERSE

START_EQUITY = 10_000.0
COST = 0.001
DB = "data/momentum_paper.db"
DAY_MS = 86_400_000


def fetch_closes(ex, symbols, need):
    out: dict[str, list[float]] = {}
    since = ex.milliseconds() - (need + 5) * DAY_MS
    for s in symbols:
        try:
            o = ex.fetch_ohlcv(s, "1d", since=since, limit=need + 5)
            if o:
                out[s] = [c[4] for c in o]
        except Exception:  # noqa: BLE001
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show book only, no rebalance")
    args = ap.parse_args()

    cfg = MomentumConfig(universe=tuple(DEFAULT_MOMENTUM_UNIVERSE))
    book = MomentumBook(cfg, db_path=DB, start_equity=START_EQUITY, cost=COST)
    ex = ccxt.bybit({"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "swap"}})
    closes = fetch_closes(ex, cfg.universe, max(cfg.lookbacks))
    s = book.update(closes, ex.milliseconds(), do_rebalance=not args.status)

    pct = (s["equity"] / s["start_equity"] - 1) * 100
    print(f"\n=== Momentum paper book — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ===")
    print(f"universe priced: {s['n_priced']}/{len(cfg.universe)}   day P&L: {s['day_pnl']:+.2f}")
    print(f"paper equity: {s['equity']:.2f}  ({pct:+.2f}% since start)")
    print(f"{'REBALANCED' if s['rebalanced'] else ('status only' if args.status else 'no rebalance due')}\n")
    print(f"LONG  ({len(s['longs'])}): " + ", ".join(x.split('/')[0] for x in s["longs"]))
    print(f"SHORT ({len(s['shorts'])}): " + ", ".join(x.split('/')[0] for x in s["shorts"]))


if __name__ == "__main__":
    main()
