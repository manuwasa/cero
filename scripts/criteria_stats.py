"""Analyze which of the 8 criteria are actually predictive.

For each criterion, computes pass-rate broken down by outcome:
  - Pass rate on WINNING signals
  - Pass rate on LOSING signals
  - "Edge" = pass_rate_winners - pass_rate_losers

If a criterion is informative, its pass rate should be **much higher on
winners than on losers**. If it passes equally on both, it's noise — adding
no signal, just consuming weight in the score formula.

Output (after enough sample):

    criterion             all   wins   losses   edge   verdict
    ───────────────────────────────────────────────────────────
    trend_h1_h4          85%   100%    80%    +20pp   predictive
    key_levels           60%    60%    61%     -1pp   noise
    poi_alert            45%    90%    20%    +70pp   strongly predictive

Use this to inform criteria_weights re-tuning. A criterion with negative
edge is actively hurting — passing more on losers than winners. A criterion
with near-zero edge is noise.

WARNING: don't act on this with <30 decided trades. Wait until your sample
is statistically meaningful. With 6 trades, "edge" numbers are random.

Usage:
    uv run python scripts/criteria_stats.py
    uv run python scripts/criteria_stats.py --horizon-hours 48
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import asc, select

from cero.config import load_config
from cero.db.models import Candle, Signal
from cero.db.session import close_db, init_db, session_factory


def _resolve(direction: str, entry: float, sl: float, tp: float, candles) -> str:
    """Return 'win' | 'loss' | 'incomplete' for one signal."""
    for c in candles:
        if direction == "long":
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
        else:
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp
        if hit_sl and hit_tp:
            return "loss"   # conservative
        if hit_sl:
            return "loss"
        if hit_tp:
            return "win"
    return "incomplete"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--tier", default="A,B")
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--candle-tf", default="5m")
    args = parser.parse_args()

    tiers = [t.strip() for t in args.tier.split(",")]
    horizon_ms = args.horizon_hours * 3600 * 1000

    cfg, _ = load_config()
    await init_db(cfg.database)

    async with session_factory()() as s:
        q = (
            select(Signal)
            .where(Signal.tier.in_(tiers))
            .where(Signal.direction.in_(["long", "short"]))
            .order_by(asc(Signal.ts))
        )
        if args.symbol:
            q = q.where(Signal.symbol == args.symbol)
        signals = (await s.execute(q)).scalars().all()

        # criterion_name → {'passes_on_win': N, 'fails_on_win': N, 'passes_on_loss': N, ...}
        from collections import defaultdict
        stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"win_pass": 0, "win_fail": 0, "loss_pass": 0, "loss_fail": 0}
        )
        outcomes = {"win": 0, "loss": 0, "incomplete": 0}

        for sig in signals:
            # Reconstruct entry/SL/TP. Use stored values if present, else skip.
            if sig.entry_price is None or sig.stop_loss is None or sig.take_profit is None:
                continue

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

            if not candles:
                outcomes["incomplete"] += 1
                continue

            result = _resolve(
                sig.direction, sig.entry_price, sig.stop_loss, sig.take_profit, candles,
            )
            outcomes[result] += 1
            if result == "incomplete":
                continue

            # Parse criteria_json
            try:
                criteria = json.loads(sig.criteria_json or "[]")
            except json.JSONDecodeError:
                continue

            for c in criteria:
                name = c.get("name")
                passed = c.get("passed")
                if not name:
                    continue
                if result == "win":
                    stats[name]["win_pass" if passed else "win_fail"] += 1
                else:
                    stats[name]["loss_pass" if passed else "loss_fail"] += 1

    await close_db()

    decided = outcomes["win"] + outcomes["loss"]
    print(f"=== Criterion edge analysis ===")
    print(f"signals analyzed:   {sum(outcomes.values())}")
    print(f"  wins:             {outcomes['win']}")
    print(f"  losses:           {outcomes['loss']}")
    print(f"  incomplete:       {outcomes['incomplete']}")
    print()

    if decided < 10:
        print(f"sample too small ({decided} decided trades).")
        print("Wait until you have 30+ before drawing conclusions.")
        print("Random variation dominates below ~30 trades — any 'edge' is noise.")
        return

    if decided < 30:
        print(f"⚠  Sample is {decided} decided trades. Stats below are SUGGESTIVE")
        print(f"   only. Don't act on them until ~30+ trades.")
        print()

    # Sort by edge magnitude
    rows = []
    for name, d in stats.items():
        wins = outcomes["win"]
        losses = outcomes["loss"]
        wp = (d["win_pass"] / wins * 100) if wins > 0 else 0.0
        lp = (d["loss_pass"] / losses * 100) if losses > 0 else 0.0
        all_pass = d["win_pass"] + d["loss_pass"]
        all_total = d["win_pass"] + d["win_fail"] + d["loss_pass"] + d["loss_fail"]
        ap = (all_pass / all_total * 100) if all_total > 0 else 0.0
        edge = wp - lp
        rows.append((name, ap, wp, lp, edge))

    rows.sort(key=lambda r: r[4], reverse=True)

    print(f"{'criterion':<22} {'all':>6} {'wins':>7} {'losses':>8} {'edge':>8}   verdict")
    print("-" * 78)
    for name, ap, wp, lp, edge in rows:
        if decided >= 30:
            if edge >= 25:
                verdict = "strongly predictive"
            elif edge >= 10:
                verdict = "predictive"
            elif edge >= -5:
                verdict = "noise"
            else:
                verdict = "ACTIVELY HARMFUL"
        else:
            verdict = "(too few samples)"
        print(f"{name:<22} {ap:>5.0f}% {wp:>6.0f}% {lp:>7.0f}% {edge:>+7.0f}pp   {verdict}")

    print()
    print("How to read this:")
    print("  - 'edge' = (pass-rate on winners) - (pass-rate on losers)")
    print("  - Positive edge → criterion correlates with wins (good)")
    print("  - Near-zero edge → criterion fires equally on wins+losses (noise)")
    print("  - Negative edge → criterion fires MORE on losers (hurts you)")
    print()
    print("If a criterion is 'ACTIVELY HARMFUL' over 50+ decided trades,")
    print("consider lowering its weight in config.yaml criteria_weights.")
    print("If it's 'strongly predictive', consider raising its weight.")
    print("Always rebalance so weights sum to 100.")


if __name__ == "__main__":
    asyncio.run(main())
