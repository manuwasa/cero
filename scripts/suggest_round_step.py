"""Suggest a round-number step for one or more symbols.

The brain's criterion 3 (key levels) uses `_ROUND_STEPS` in
cero/brain/scheduler.py to know what counts as a "psychological" round
price for each symbol. The rule of thumb is ~0.5% of current price,
rounded to a clean decimal multiple.

Run this when adding a new symbol — copy the suggested step into
`_ROUND_STEPS`. Examples shown for the three configured symbols + any
extras you pass on the command line.

Usage:
    uv run python scripts/suggest_round_step.py
    uv run python scripts/suggest_round_step.py DOGE/USDT:USDT AVAX/USDT:USDT
"""
from __future__ import annotations

import asyncio
import math
import sys

from cero.config import load_config
from cero.data.exchange import ExchangeClient


def suggest_step(price: float, target_pct: float = 0.005) -> float:
    """Return a clean step that's roughly `target_pct` of `price`.

    Strategy: target an absolute step of `price * target_pct`, then snap to
    the nearest "clean" decimal — 1, 2, 5 times a power of ten."""
    if price <= 0:
        return 1.0
    raw = price * target_pct
    # log10 gives us the order of magnitude
    exponent = math.floor(math.log10(raw))
    base = 10 ** exponent
    # Pick whichever of {1, 2, 5} × base is closest to raw
    candidates = [1 * base, 2 * base, 5 * base, 10 * base]
    return min(candidates, key=lambda c: abs(c - raw))


def _fmt(n: float) -> str:
    if n >= 1:
        return f"{n:g}"
    # show enough decimals for sub-1 prices to be readable
    return f"{n:.10f}".rstrip("0").rstrip(".")


async def main() -> None:
    cfg, secrets = load_config()
    cli_symbols = sys.argv[1:]
    symbols = list(dict.fromkeys(list(cfg.symbols) + cli_symbols))   # dedupe, keep order

    async with ExchangeClient(cfg, secrets) as ex:
        print(f"{'symbol':<25} {'price':>14}   {'step (~0.5%)':>14}   {'step %':>8}")
        print("-" * 70)
        for sym in symbols:
            try:
                ex.normalize_symbol(sym)
            except Exception:
                print(f"{sym:<25} {'NOT LISTED':>14}")
                continue
            try:
                t = await ex.fetch_ticker(sym)
            except Exception as e:
                print(f"{sym:<25} fetch failed: {e}")
                continue
            step = suggest_step(t.last)
            pct = (step / t.last) * 100 if t.last > 0 else 0
            print(f"{sym:<25} {_fmt(t.last):>14}   {_fmt(step):>14}   {pct:>7.2f}%")

    print()
    print("Paste into cero/brain/scheduler.py -> _ROUND_STEPS:")
    print()
    print('_ROUND_STEPS: dict[str, float] = {')
    async with ExchangeClient(cfg, secrets) as ex:
        for sym in symbols:
            try:
                t = await ex.fetch_ticker(sym)
                step = suggest_step(t.last)
                print(f'    "{sym}": {_fmt(step)},')
            except Exception:
                pass
    print('}')


if __name__ == "__main__":
    asyncio.run(main())
