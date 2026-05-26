"""Backtest signals from the DB — what would have happened if every tier-A/B
signal had been placed at its stated entry/SL/TP?

For each candidate signal, we walk forward through 5-minute candles starting
at signal.ts and determine the first level touched:
  - long:  hit SL first (low <= sl)  → loss = -1R
           hit TP first (high >= tp) → win  = +R × rr
  - short: hit SL first (high >= sl) → loss
           hit TP first (low <= tp)  → win
  - neither within `--horizon-hours` → 'incomplete'

R is the stop distance (entry_price - stop_loss in absolute value).
rr is take_profit distance / stop distance, typically 2.0 in Cero.

Both-on-same-candle (ambiguous) is conservatively counted as a loss — this
underestimates real outcomes slightly but never overstates them.

What this backtester DOES model:
  - Entry at signal.entry_price (the brain's intended fill)
  - SL/TP outcomes from candle highs/lows post-signal
  - Per-tier breakdown
  - Stability check (first-half vs second-half win rate)

What it does NOT model:
  - Slippage on entry (real fills are 0.05-0.5% off)
  - Slippage on SL/TP (similar magnitude)
  - Partial fills (IOC cancellation)
  - Exchange fees (~0.06% per leg)

Output is an honest upper bound on profitability. Real auto-mode results
will be 0.2-1% worse per trade due to these.

Usage:
    uv run python scripts/backtest_signals.py
    uv run python scripts/backtest_signals.py --horizon-hours 48 --rr 2.0
    uv run python scripts/backtest_signals.py --symbol ETH/USDT:USDT --tier B
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import asc, select

from cero.config import load_config
from cero.db.models import Candle, Signal
from cero.db.session import close_db, init_db, session_factory


@dataclass
class Outcome:
    signal_id: int
    symbol: str
    tier: str
    direction: str
    score: int
    ts: int
    entry: float
    sl: float
    tp: float
    result: str             # 'win' | 'loss' | 'incomplete'
    bars_to_resolve: int = 0
    r_multiple: float = 0.0


def _resolve(
    direction: str, entry: float, sl: float, tp: float,
    candles: list[Candle], rr: float,
) -> tuple[str, int, float]:
    """Walk candles forward; return (result, bars_used, r_multiple).
    Same-bar ambiguity counts as a loss (conservative)."""
    for i, c in enumerate(candles):
        if direction == "long":
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
        else:  # short
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp
        if hit_sl and hit_tp:
            return "loss", i + 1, -1.0
        if hit_sl:
            return "loss", i + 1, -1.0
        if hit_tp:
            return "win", i + 1, rr
    return "incomplete", len(candles), 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None, help="filter to one symbol")
    parser.add_argument(
        "--tier", default="A,B",
        help="comma-separated tiers to evaluate (default A,B)",
    )
    parser.add_argument(
        "--horizon-hours", type=int, default=24,
        help="how long after signal to wait before calling it incomplete",
    )
    parser.add_argument(
        "--rr", type=float, default=2.0,
        help="reward:risk ratio (used to compute r_multiple, default 2.0)",
    )
    parser.add_argument(
        "--candle-tf", default="5m",
        help="which candle timeframe to use for resolution (default 5m)",
    )
    parser.add_argument(
        "--slippage-pct", type=float, default=0.1,
        help="slippage as percent of entry price applied to each fill "
             "(default 0.1%). Subtracted from r_multiple per trade.",
    )
    parser.add_argument(
        "--fee-pct", type=float, default=0.06,
        help="taker fee per leg as percent of notional (default 0.06%% — "
             "bybit perp taker rate). 2 legs per trade (entry + exit).",
    )
    parser.add_argument(
        "--no-costs", action="store_true",
        help="show ideal-world numbers without slippage/fees (the old behavior)",
    )
    args = parser.parse_args()

    tiers = [t.strip() for t in args.tier.split(",")]
    horizon_ms = args.horizon_hours * 3600 * 1000

    cfg, _ = load_config()
    await init_db(cfg.database)

    async with session_factory()() as s:
        # Pull candidate signals
        sig_q = (
            select(Signal)
            .where(Signal.tier.in_(tiers))
            .where(Signal.direction.in_(["long", "short"]))
            .order_by(asc(Signal.ts))
        )
        if args.symbol:
            sig_q = sig_q.where(Signal.symbol == args.symbol)
        signals = (await s.execute(sig_q)).scalars().all()

        if not signals:
            print(
                f"no signals match (tier in {tiers}, "
                f"symbol={args.symbol or 'ANY'}) — nothing to backtest"
            )
            await close_db()
            return

        outcomes: list[Outcome] = []
        for sig in signals:
            # Pull candles after signal.ts, capped by horizon
            end_ms = sig.ts + horizon_ms
            candles = (
                await s.execute(
                    select(Candle)
                    .where(Candle.symbol == sig.symbol)
                    .where(Candle.timeframe == args.candle_tf)
                    .where(Candle.open_time >= sig.ts)
                    .where(Candle.open_time <= end_ms)
                    .order_by(asc(Candle.open_time))
                )
            ).scalars().all()

            # Use the brain's stored entry/SL/TP if available (added to the
            # signals table to support accurate backtesting). Older rows are
            # nullable — fall back to a rough 1% reconstruction.
            entry = sig.entry_price
            sl = sig.stop_loss
            tp = sig.take_profit
            if entry is None or sl is None or tp is None:
                if not candles:
                    outcomes.append(Outcome(
                        signal_id=sig.id, symbol=sig.symbol, tier=sig.tier,
                        direction=sig.direction, score=sig.score, ts=sig.ts,
                        entry=0.0, sl=0.0, tp=0.0, result="incomplete",
                    ))
                    continue
                entry = candles[0].open
                stop_dist = entry * 0.01
                if sig.direction == "long":
                    sl = entry - stop_dist
                    tp = entry + args.rr * stop_dist
                else:
                    sl = entry + stop_dist
                    tp = entry - args.rr * stop_dist

            if not candles:
                outcomes.append(Outcome(
                    signal_id=sig.id, symbol=sig.symbol, tier=sig.tier,
                    direction=sig.direction, score=sig.score, ts=sig.ts,
                    entry=entry, sl=sl, tp=tp, result="incomplete",
                ))
                continue

            # Compute actual rr from the stored prices so the r_multiple is
            # correct even when the brain used non-2.0 rr.
            stop_dist = abs(entry - sl)
            tp_dist = abs(entry - tp)
            actual_rr = (tp_dist / stop_dist) if stop_dist > 0 else args.rr

            result, bars, r_mult = _resolve(
                sig.direction, entry, sl, tp, candles, actual_rr,
            )
            # Apply realistic execution costs unless --no-costs was passed.
            # Costs subtract from r_mult regardless of outcome (you pay on
            # every trade, win or lose).
            if not args.no_costs and result in ("win", "loss"):
                stop_dist = abs(entry - sl)
                # Slippage: applied on both legs (entry + exit). Expressed in
                # absolute price, then converted to R units.
                slip_per_leg = entry * (args.slippage_pct / 100)
                slippage_r = (slip_per_leg * 2) / stop_dist if stop_dist > 0 else 0
                # Fees: same idea — percent of notional, applied twice.
                fees_per_leg = entry * (args.fee_pct / 100)
                fees_r = (fees_per_leg * 2) / stop_dist if stop_dist > 0 else 0
                r_mult -= (slippage_r + fees_r)
            outcomes.append(Outcome(
                signal_id=sig.id, symbol=sig.symbol, tier=sig.tier,
                direction=sig.direction, score=sig.score, ts=sig.ts,
                entry=entry, sl=sl, tp=tp, result=result,
                bars_to_resolve=bars, r_multiple=r_mult,
            ))

    await close_db()

    # ── report ──────────────────────────────────────────────────────────
    n = len(outcomes)
    wins = [o for o in outcomes if o.result == "win"]
    losses = [o for o in outcomes if o.result == "loss"]
    incomplete = [o for o in outcomes if o.result == "incomplete"]
    decided = wins + losses

    print(f"=== Cero signal backtest ===")
    print(f"signals evaluated:  {n}")
    print(f"  decided:          {len(decided)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  incomplete:       {len(incomplete)}  (no SL/TP hit within "
          f"{args.horizon_hours}h)")
    print(f"  candle tf used:   {args.candle_tf}")
    print(f"  rr assumed:       {args.rr}")
    print()

    if not decided:
        print("no decided outcomes — sample too small or candles missing.")
        return

    wr = len(wins) / len(decided) * 100
    total_r = sum(o.r_multiple for o in decided)
    # Profit factor: gross wins / gross losses (in R units)
    gross_wins = sum(o.r_multiple for o in wins)
    gross_losses = abs(sum(o.r_multiple for o in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    print(f"win rate:           {wr:.1f}% ({len(wins)}/{len(decided)})")
    print(f"total R:            {total_r:+.2f}")
    print(f"profit factor:      {pf:.2f}")

    # Max consecutive losses
    consec = 0
    max_consec = 0
    decided_sorted = sorted(decided, key=lambda o: o.ts)
    for o in decided_sorted:
        if o.result == "loss":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
    print(f"max consec losses:  {max_consec}")

    # Stability — first half vs second half (only meaningful with >= 20)
    if len(decided) >= 20:
        half = len(decided) // 2
        first = decided_sorted[:half]
        second = decided_sorted[half:]
        wr1 = sum(1 for o in first if o.result == "win") / len(first) * 100
        wr2 = sum(1 for o in second if o.result == "win") / len(second) * 100
        print(f"first-half WR:      {wr1:.1f}%")
        print(f"last-half WR:       {wr2:.1f}%")
        print(f"stability:          {abs(wr1 - wr2):.1f}pp difference")

    # Per-tier
    print()
    print("per tier:")
    for tier in tiers:
        tier_decided = [o for o in decided if o.tier == tier]
        if not tier_decided:
            print(f"  {tier}: none")
            continue
        tw = sum(1 for o in tier_decided if o.result == "win")
        twr = tw / len(tier_decided) * 100
        tr = sum(o.r_multiple for o in tier_decided)
        print(f"  {tier}: {len(tier_decided)} trades, WR {twr:.1f}%, total {tr:+.2f}R")

    # Per symbol
    print()
    print("per symbol:")
    by_symbol: dict[str, list[Outcome]] = {}
    for o in decided:
        by_symbol.setdefault(o.symbol, []).append(o)
    for sym in sorted(by_symbol):
        os_ = by_symbol[sym]
        sw = sum(1 for o in os_ if o.result == "win")
        swr = sw / len(os_) * 100
        sr = sum(o.r_multiple for o in os_)
        print(f"  {sym}: {len(os_)} trades, WR {swr:.1f}%, total {sr:+.2f}R")

    print()
    print("=== Validation gate check ===")
    pass_count = len(decided) >= 200
    pass_wr = wr >= 55.0
    pass_pf = pf >= 1.5
    pass_stable = (
        len(decided) >= 20 and abs(
            (sum(1 for o in decided_sorted[: len(decided) // 2] if o.result == "win") / (len(decided) // 2) * 100)
            - (sum(1 for o in decided_sorted[len(decided) // 2 :] if o.result == "win") / (len(decided) - len(decided) // 2) * 100)
        ) <= 5
    )
    print(f"  count >= 200:     {'PASS' if pass_count else 'fail'}  ({len(decided)})")
    print(f"  win rate >= 55%:  {'PASS' if pass_wr else 'fail'}  ({wr:.1f}%)")
    print(f"  PF >= 1.5:        {'PASS' if pass_pf else 'fail'}  ({pf:.2f})")
    print(f"  stable (<=5pp):   {'PASS' if pass_stable else 'fail'}")
    print()
    if all([pass_count, pass_wr, pass_pf, pass_stable]):
        print("OVERALL: gate PASSED — eligible to consider Stage 1 mainnet.")
    else:
        print("OVERALL: gate NOT passed yet. Keep collecting trades.")
    print()
    print("Costs modeled:")
    if args.no_costs:
        print("  ⚠  --no-costs flag set: this is the IDEAL-WORLD number.")
        print("     Real auto-trading will be 0.2-0.5% worse per trade.")
    else:
        print(f"  slippage: {args.slippage_pct}% per fill × 2 legs")
        print(f"  fees:     {args.fee_pct}% per fill × 2 legs (bybit taker)")
        print(f"  partial fills not modeled — testnet's thin liquidity will")
        print(f"  make real auto-mode outcomes slightly worse still.")
    print()
    print("Run with --no-costs to compare against the ideal-world number.")


if __name__ == "__main__":
    asyncio.run(main())
