"""Does smc_trend's entry logic actually have edge — or is it no better than naive?

Holds EXITS identical for all three entry rules (same ATR-clamped stop, rr 2.0 —
the exact _stop_and_target the live brain uses) and the same mainnet data, stepping,
and costs. Only the choice of WHEN/WHICH-WAY to enter differs:

  smc_trend   — the real 8-criteria strategy (tier A/B signals only)
  HTF-trend   — enter with the H1+H4 trend whenever aligned (criterion 1 alone)
  random      — coin-flip direction every step (the control)

Reading it:
  smc ~= HTF-trend  -> criteria 2..8 add nothing on top of "trade the trend"
  smc ~= random     -> the entries have no edge at all; rebuild from scratch
  smc >  random but still PF<1 -> weak signal to refine

Reuses scripts/backtest_mainnet.py so the replay stays single-sourced.

Usage:
    uv run python scripts/baseline_compare.py
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_mainnet as bt  # noqa: E402

from cero.brain.criteria import MarketContext  # noqa: E402
from cero.brain.indicators import classify_trend  # noqa: E402
from cero.brain.signals import _stop_and_target  # noqa: E402
from cero.brain.strategies.base import StrategyContext  # noqa: E402
from cero.brain.strategies.smc_trend import SmcTrendStrategy  # noqa: E402
from cero.config import load_config  # noqa: E402


async def generate(rule, data, cfg, args):
    """Walk the same steps as the live brain; emit one signal dict per entry."""
    out = []
    step = max(1, args.step_min // 5)
    gate = bt.RiskGate(cfg.risk, cfg.news)
    smc = SmcTrendStrategy()
    rng = random.Random(42)
    for sym in data:
        c5, ot5 = data[sym]["5m"]
        if len(c5) < 100:
            continue
        first = bisect.bisect_left(ot5, ot5[0] + args.warmup_days * 86_400_000)
        for i in range(first, len(c5), step):
            T = ot5[i]
            dt = T + bt.TF_MS["5m"]
            cdict = {}
            for tf in bt.ALL_TFS:
                cands, ots = data[sym][tf]
                hi = bisect.bisect_right(ots, dt - bt.TF_MS[tf])
                if hi <= 0:
                    continue
                cdict[tf] = cands[max(0, hi - bt.CAP[tf]):hi]
            c1h = cdict.get("1h") or []
            if len(c1h) < 55:
                continue
            atrh1 = bt.atr_h1_of(c1h)
            if atrh1 <= 0:
                continue
            ctx = MarketContext(symbol=sym, now_ms=T, candles=cdict,
                                weights=cfg.criteria_weights,
                                round_step=bt._ROUND_STEPS.get(sym, 1000.0))
            price = ctx.current_price

            direction = None
            if rule == "smc_trend":
                sctx = StrategyContext(
                    market=ctx, risk_gate=gate, equity=10_000.0, atr_h1=atrh1,
                    mode="signal_only", open_positions=0, today_realized=0.0,
                    today_consecutive_losses=0, in_blackout=False, blackout_name=None,
                )
                sig = await smc.evaluate(sctx)
                if sig and sig.tier in ("A", "B") and sig.direction in ("long", "short"):
                    direction = sig.direction
            elif rule == "HTF-trend":
                c4h = cdict.get("4h") or []
                t1 = classify_trend([c.close for c in c1h])
                t4 = classify_trend([c.close for c in c4h]) if len(c4h) >= 55 else "flat"
                if t1 in ("up", "down") and t1 == t4:
                    direction = "long" if t1 == "up" else "short"
            else:  # random
                direction = rng.choice(["long", "short"])

            if direction is None:
                continue
            sl, tp = _stop_and_target(direction, price, atrh1, rr=2.0)
            out.append({"strategy": rule, "symbol": sym, "ts": T, "tier": "-",
                        "direction": direction, "entry": price, "sl": sl, "tp": tp,
                        "base_stop": abs(price - sl)})
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_mainnet.db")
    ap.add_argument("--step-min", type=int, default=15)
    ap.add_argument("--warmup-days", type=int, default=7)
    ap.add_argument("--horizon-hours", type=int, default=24)
    ap.add_argument("--no-costs", action="store_true")
    ap.add_argument("--symbols", default=",".join(bt._ROUND_STEPS))
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    horizon_ms = args.horizon_hours * 3600_000
    cfg, _ = load_config()
    data = bt.load_candles(args.db, symbols)

    cost = "no-costs" if args.no_costs else "realistic costs"
    print(f"\n=== Entry-edge baseline (mainnet, non-overlap, {cost}) ===")
    print(f"same ATR-clamped exits (rr 2.0) for all three; step={args.step_min}m "
          f"horizon={args.horizon_hours}h\n")
    print(f"{'entry rule':<14}{'signals':>8}{'dec':>6}{'WR':>8}{'totR':>9}{'PF':>7}")
    print("-" * 52)
    results = {}
    for rule in ("smc_trend", "HTF-trend", "random"):
        sigs = await generate(rule, data, cfg, args)
        for s in sigs:
            fwd = bt.forward_bars(data, s["symbol"], s["ts"], horizon_ms)
            res, r, rt = bt.resolve_costed(s, s["sl"], s["tp"], fwd, args.no_costs)
            s.update(result=res, r=r, resolve_ts=rt)
        taken = bt.apply_non_overlap(sigs)
        n, w, wr, totr, pf = bt.stats(taken)
        results[rule] = (wr, pf, totr)
        pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{rule:<14}{len(sigs):>8}{n:>6}{wr:>7.1f}%{totr:>+9.1f}{pfs:>7}")

    print()
    smc_pf = results["smc_trend"][1]
    trend_pf = results["HTF-trend"][1]
    rand_pf = results["random"][1]
    if smc_pf <= rand_pf + 0.05:
        print("VERDICT: smc_trend is no better than random -> the entries have no edge.")
        print("         Rebuild entry logic from scratch; the criteria aren't selecting.")
    elif abs(smc_pf - trend_pf) <= 0.05:
        print("VERDICT: smc_trend ~= 'just trade the HTF trend' -> criteria 2..8 add nothing.")
        print("         The edge (if any) is only in the trend filter; the rest is overhead.")
    else:
        print("VERDICT: smc_trend beats the baselines -> the criteria carry real signal.")
        print("         Refine (don't replace) the entries.")
    print("(all three still need PF >= ~1 to be profitable before costs even matter.)")


if __name__ == "__main__":
    asyncio.run(main())
