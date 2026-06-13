"""Momentum review — turn the paper book + logs into an actual performance review.

`momentum_check.py` is a *snapshot* (where the book stands right now). This is
the *review*: it reconstructs the equity curve and scores it — total return,
peak/trough, max drawdown, cycle volatility, days live, rebalances, turnover —
plus a BTC buy-and-hold benchmark over the same window (the honest bar: a
long/short book should earn its complexity vs. just holding).

The metrics live in cero.brain.momentum.review_book() so the Telegram /review
command shares the exact same computation. This script just prints them and adds
the benchmark. Read-only; safe to run while Cero is live.

Usage:
    .venv/bin/python scripts/momentum_review.py
    .venv/bin/python scripts/momentum_review.py --no-benchmark        # offline only
    .venv/bin/python scripts/momentum_review.py --db data/momentum_paper.db --log logs/cero.log
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

try:  # block/arrow glyphs render on Windows consoles too
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from cero.brain.momentum import review_book
from cero.config import load_config


async def btc_benchmark(cfg, secrets, symbol: str, days: int) -> float | None:
    """Buy-and-hold return of `symbol` over roughly the last `days` days, via the
    same exchange wrapper the engine uses for chart data."""
    from cero.data.exchange import ExchangeClient

    async with ExchangeClient(cfg, secrets) as ex:
        candles = await ex.fetch_ohlcv(symbol, "1d", limit=days + 3)
    closes = [c.close for c in candles]
    if len(closes) < 2:
        return None
    window = closes[-(days + 1):] if days + 1 <= len(closes) else closes
    return window[-1] / window[0] - 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only momentum paper-trading review.")
    ap.add_argument("--db", default="data/momentum_paper.db")
    ap.add_argument("--log", default="logs/cero.log")
    ap.add_argument("--no-benchmark", action="store_true",
                    help="skip the BTC hold benchmark (no network needed)")
    ap.add_argument("--benchmark-symbol", default="BTC/USDT:USDT")
    args = ap.parse_args()

    cfg, secrets = load_config()
    r = review_book(args.db, args.log)
    if not r:
        print(f"no momentum book at {args.db} — has the engine run a cycle yet?")
        return

    print(f"=== Cero momentum review — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ===\n")

    span = r.get("span_days", 0.0)
    if r["has_curve"]:
        print(f"window: {r['first_dt']:%Y-%m-%d %H:%M} → {r['last_dt']:%Y-%m-%d %H:%M}  "
              f"({span:.1f} days, {r['n_cycles']} cycles, {r['n_rebalances']} rebalances)\n")
        print(f"equity:        {r['equity']:.2f}   start {r['start']:.0f}    →  {r['total_ret'] * 100:+.2f}%")
        print(f"peak:          {r['peak'][0]:.0f}  ({r['peak'][1]:%Y-%m-%d %H:%M})")
        print(f"trough:        {r['trough'][0]:.0f}  ({r['trough'][1]:%Y-%m-%d %H:%M})")
        print(f"max drawdown:  {r['max_drawdown'] * 100:+.1f}%   (worst peak→trough on the curve)")
        print(f"cycle vol:     {r['cycle_vol'] * 100:.2f}%   (std of ~6h equity moves)")
        print(f"turnover:      {r['turnover']:,.0f} traded over {r['n_fills']} fills "
              f"({r['turnover_x']:.1f}× start equity)")
        print(f"\ncurve: {r['sparkline']}")
    else:
        print(f"equity:        {r['equity']:.2f}   start {r['start']:.0f}    →  {r['total_ret'] * 100:+.2f}%")
        print(f"turnover:      {r['turnover']:,.0f} traded over {r['n_fills']} fills")
        print(f"(no [MOM] lines in {args.log} — can't draw the curve or drawdown)")

    if not args.no_benchmark and span >= 0.5:
        import asyncio
        days = max(1, round(span))
        bench: float | None = None
        try:
            bench = asyncio.run(btc_benchmark(cfg, secrets, args.benchmark_symbol, days))
        except Exception as e:  # noqa: BLE001 — network/geo issues shouldn't kill the review
            print(f"\nbenchmark: skipped — {args.benchmark_symbol} unreachable ({type(e).__name__})")
        if bench is not None:
            base = args.benchmark_symbol.split("/")[0]
            gap = (r["total_ret"] - bench) * 100
            verb = "beat" if gap >= 0 else "lagged"
            print(f"\nbenchmark (hold {base}, ~{days}d):  {bench * 100:+.2f}%")
            print(f"  → momentum {r['total_ret'] * 100:+.2f}% {verb} buy-and-hold by {abs(gap):.1f} pts")

    print(f"\nbook: {r['n_longs']} long / {r['n_shorts']} short")
    if span < 14:
        print(f"\n** {span:.0f}d of data is NOISE — read this as a sanity check, not a verdict. **")
        print("   Judge the edge over weeks, ideally across an up- AND a down-market. Paper, no real money.")


if __name__ == "__main__":
    main()
