"""List all USDT perp symbols available on the configured exchange.

Filters to ccxt unified `BASE/USDT:USDT` format (linear USDT-margined perps),
which is what Cero trades. Pass --grep BTC (or any string) to filter the
output to just symbols containing that substring.

Usage:
    uv run python scripts/list_symbols.py
    uv run python scripts/list_symbols.py --grep DOGE
    uv run python scripts/list_symbols.py --sort  # alphabetical
"""
from __future__ import annotations

import argparse
import asyncio

from cero.config import load_config
from cero.data.exchange import ExchangeClient


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grep", default="", help="case-insensitive filter")
    parser.add_argument("--sort", action="store_true", help="sort alphabetically")
    args = parser.parse_args()

    cfg, secrets = load_config()
    async with ExchangeClient(cfg, secrets) as ex:
        all_symbols = list(ex._ccxt.markets.keys())
        usdt_perps = [s for s in all_symbols if s.endswith("/USDT:USDT")]
        if args.grep:
            needle = args.grep.upper()
            usdt_perps = [s for s in usdt_perps if needle in s.upper()]
        if args.sort:
            usdt_perps.sort()

        print(f"{ex.exch_cfg.name} (testnet={ex.exch_cfg.testnet}) — "
              f"{len(usdt_perps)} USDT perps"
              + (f" matching {args.grep!r}" if args.grep else ""))
        print()
        # Three columns to fit more on screen
        per_row = 3
        col_width = max((len(s) for s in usdt_perps), default=0) + 2
        for i in range(0, len(usdt_perps), per_row):
            row = usdt_perps[i : i + per_row]
            print("  " + "".join(s.ljust(col_width) for s in row))


if __name__ == "__main__":
    asyncio.run(main())
