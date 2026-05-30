"""Offline backtest on REAL (mainnet) data.

This is the validation tool Cero never had: it replays the *actual* strategies
(smc_trend, mean_reversion) over real historical candles and measures their edge.
Unlike backtest_signals.py — which scores signals the live bot already emitted
from corrupt testnet prices — this regenerates signals from clean mainnet data,
so the WR/R/PF numbers are real.

How it reproduces the live brain (cero/brain/scheduler.py) faithfully:
  - Steps forward over 5m bars (the live trigger timeframe).
  - At each step T, builds a MarketContext from ONLY candles closed by T
    (no lookahead), capped per timeframe like the live DB.
  - Computes atr_h1, wraps a StrategyContext, calls each strategy's evaluate().
  - Resolves every tier-A/B signal on the forward 5m candles (SL-first, same-bar
    = loss), applying realistic slippage+fees — identical to backtest_signals.py.

Two views are reported:
  RAW          — every tier-A/B signal scored independently (matches the existing
                 validation-gate methodology; inflated by overlapping duplicates).
  NON-OVERLAP  — one position per (strategy, symbol) at a time: a new signal is
                 skipped while a prior one is still open. The realistic read.

--sweep mode: hold the strategy's ENTRIES fixed and sweep exit parameters
  (stop-distance multiplier x reward:risk). Answers "are the entries salvageable
  with better exits, or hopeless regardless?" — a diagnostic, NOT a rule change.

Prereq: run scripts/backfill_mainnet.py first to populate data/cero_mainnet.db.

Usage:
    uv run python scripts/backtest_mainnet.py
    uv run python scripts/backtest_mainnet.py --step-min 5         # full fidelity
    uv run python scripts/backtest_mainnet.py --sweep              # exit-param sweep (smc_trend)
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import sqlite3
from collections import defaultdict

import numpy as np

from cero.brain.indicators import atr
from cero.brain.criteria import MarketContext
from cero.brain.risk import RiskGate
from cero.brain.strategies import ALL_STRATEGIES
from cero.brain.strategies.base import StrategyContext
from cero.config import load_config
from cero.data.exchange import Candle

# Mirror of scheduler._ROUND_STEPS — keep in sync if the live values change.
_ROUND_STEPS = {
    "BTC/USDT:USDT": 500.0, "ETH/USDT:USDT": 10.0,
    "SOL/USDT:USDT": 0.5, "BNB/USDT:USDT": 2.0,
}
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
CAP = {"5m": 200, "15m": 160, "30m": 160, "1h": 160, "4h": 120, "1d": 60}
ALL_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]
SLIP_PCT, FEE_PCT = 0.1, 0.06


def load_candles(db: str, symbols: list[str]):
    """-> {symbol: {tf: (candles:list[Candle], open_times:list[int])}}"""
    con = sqlite3.connect(db)
    out: dict = {}
    for sym in symbols:
        out[sym] = {}
        for tf in ALL_TFS:
            rows = con.execute(
                "SELECT open_time,open,high,low,close,volume FROM candles "
                "WHERE symbol=? AND timeframe=? ORDER BY open_time", (sym, tf),
            ).fetchall()
            cands = [Candle(symbol=sym, timeframe=tf, open_time=r[0], open=r[1],
                            high=r[2], low=r[3], close=r[4], volume=r[5]) for r in rows]
            out[sym][tf] = (cands, [c.open_time for c in cands])
    con.close()
    return out


def atr_h1_of(c1h: list[Candle]) -> float:
    if len(c1h) < 15:
        return 0.0
    a = atr([c.high for c in c1h], [c.low for c in c1h], [c.close for c in c1h], 14)
    return float(a[-1]) if not np.isnan(a[-1]) else 0.0


def resolve(direction, entry, sl, tp, fwd) -> tuple[str, float, int]:
    """fwd = [(low, high), ...] forward 5m bars. SL-first same-bar = loss."""
    if None in (entry, sl, tp):
        return "incomplete", 0.0, 0
    sd = abs(entry - sl)
    rr = abs(entry - tp) / sd if sd > 0 else 2.0
    for i, (low, high) in enumerate(fwd):
        if direction == "long":
            if low <= sl:
                return "loss", -1.0, i + 1
            if high >= tp:
                return "win", rr, i + 1
        else:
            if high >= sl:
                return "loss", -1.0, i + 1
            if low <= tp:
                return "win", rr, i + 1
    return "incomplete", 0.0, len(fwd)


def forward_bars(data, symbol, ts, horizon_ms) -> list[tuple[float, float]]:
    c5, ot5 = data[symbol]["5m"]
    j = bisect.bisect_right(ot5, ts)
    out = []
    for k in range(j, len(c5)):
        if ot5[k] > ts + horizon_ms:
            break
        out.append((c5[k].low, c5[k].high))
    return out


def resolve_costed(s, sl, tp, fwd, no_costs):
    res, r, bars = resolve(s["direction"], s["entry"], sl, tp, fwd)
    r_real = r
    if not no_costs and res in ("win", "loss"):
        sd = abs(s["entry"] - sl)
        if sd > 0:
            r_real = r - (s["entry"] * (SLIP_PCT + FEE_PCT) / 100 * 2) / sd
    return res, r_real, s["ts"] + bars * TF_MS["5m"]


def apply_non_overlap(rows) -> list[dict]:
    """One position per symbol at a time. Returns the taken (decided) rows."""
    by_sym = defaultdict(list)
    for s in rows:
        by_sym[s["symbol"]].append(s)
    taken = []
    for lst in by_sym.values():
        lst.sort(key=lambda x: x["ts"])
        free_at = 0
        for s in lst:
            if s["result"] == "incomplete":
                continue
            if s["ts"] >= free_at:
                taken.append(s)
                free_at = s["resolve_ts"]
    return taken


def stats(rows):
    dec = [s for s in rows if s["result"] in ("win", "loss")]
    n = len(dec)
    w = sum(1 for s in dec if s["result"] == "win")
    wr = w / n * 100 if n else 0.0
    gw = sum(s["r"] for s in dec if s["r"] > 0)
    gl = abs(sum(s["r"] for s in dec if s["r"] <= 0))
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return n, w, wr, sum(s["r"] for s in dec), pf


async def collect_signals(data, strategies, cfg, args):
    """Replay the brain; return raw tier-A/B signal dicts (unresolved)."""
    signals: list[dict] = []
    steps_run = 0
    step = max(1, args.step_min // 5)
    gate = RiskGate(cfg.risk, cfg.news)
    for sym in data:
        c5, ot5 = data[sym]["5m"]
        if len(c5) < 100:
            continue
        first = bisect.bisect_left(ot5, ot5[0] + args.warmup_days * 86_400_000)
        for i in range(first, len(c5), step):
            T = ot5[i]
            dt = T + TF_MS["5m"]
            cdict = {}
            for tf in ALL_TFS:
                cands, ots = data[sym][tf]
                hi = bisect.bisect_right(ots, dt - TF_MS[tf])
                if hi <= 0:
                    continue
                cdict[tf] = cands[max(0, hi - CAP[tf]):hi]
            c1h = cdict.get("1h") or []
            if len(c1h) < 55:
                continue
            steps_run += 1
            ctx = MarketContext(symbol=sym, now_ms=T, candles=cdict,
                                weights=cfg.criteria_weights,
                                round_step=_ROUND_STEPS.get(sym, 1000.0))
            sctx = StrategyContext(
                market=ctx, risk_gate=gate, equity=10_000.0, atr_h1=atr_h1_of(c1h),
                mode="signal_only", open_positions=0, today_realized=0.0,
                today_consecutive_losses=0, in_blackout=False, blackout_name=None,
            )
            for strat in strategies:
                try:
                    sig = await strat.evaluate(sctx)
                except Exception:  # noqa: BLE001
                    continue
                if sig is None or sig.tier not in ("A", "B") or sig.direction not in ("long", "short"):
                    continue
                signals.append({
                    "strategy": strat.name, "symbol": sym, "ts": T, "tier": sig.tier,
                    "direction": sig.direction, "entry": sig.entry_price,
                    "sl": sig.stop_loss, "tp": sig.take_profit,
                    "base_stop": abs(sig.entry_price - sig.stop_loss),
                })
    return signals, steps_run


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_mainnet.db")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--step-min", type=int, default=15)
    ap.add_argument("--warmup-days", type=int, default=7)
    ap.add_argument("--horizon-hours", type=int, default=24)
    ap.add_argument("--no-costs", action="store_true")
    ap.add_argument("--symbols", default=",".join(_ROUND_STEPS))
    ap.add_argument("--sweep", action="store_true",
                    help="hold entries fixed, sweep stop x rr exits (diagnostic)")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    horizon_ms = args.horizon_hours * 3600_000
    cfg, _ = load_config()
    data = load_candles(args.db, symbols)

    if args.sweep:
        target = args.strategy or cfg.primary_strategy
        strategies = [s for s in ALL_STRATEGIES if s.name == target]
        signals, steps_run = await collect_signals(data, strategies, cfg, args)
        _sweep(signals, data, target, horizon_ms, args, steps_run)
        return

    strategies = [s for s in ALL_STRATEGIES
                  if args.strategy is None or s.name == args.strategy]
    signals, steps_run = await collect_signals(data, strategies, cfg, args)
    for s in signals:
        fwd = forward_bars(data, s["symbol"], s["ts"], horizon_ms)
        res, r, rt = resolve_costed(s, s["sl"], s["tp"], fwd, args.no_costs)
        s.update(result=res, r=r, resolve_ts=rt)
    _report(signals, strategies, cfg, args, steps_run)


def _line(label, rows):
    n, w, wr, totr, pf = stats(rows)
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"  {label:<26} {n:>4} dec  {w:>3}W  WR {wr:>5.1f}%  R {totr:>+8.2f}  PF {pfs}")


def _report(signals, strategies, cfg, args, steps_run):
    cost = "no-costs (ideal)" if args.no_costs else "realistic (slippage+fees)"
    print(f"\n=== Offline mainnet backtest === db={args.db}")
    print(f"steps evaluated: {steps_run}   step={args.step_min}m   horizon={args.horizon_hours}h   {cost}")
    print(f"signals generated (tier A/B): {len(signals)}\n")
    for strat in strategies:
        srows = [s for s in signals if s["strategy"] == strat.name]
        if not srows:
            print(f"### {strat.name}: no tier-A/B signals\n"); continue
        taken = apply_non_overlap(srows)
        primary = " (PRIMARY)" if strat.name == cfg.primary_strategy else " (shadow)"
        print(f"### {strat.name}{primary}")
        _line("RAW (all signals)", srows)
        _line("NON-OVERLAP (realistic)", taken)
        for t in ("A", "B"):
            tr = [s for s in taken if s["tier"] == t]
            if tr:
                _line(f"  tier {t}", tr)
        for sym in sorted({s["symbol"] for s in taken}):
            _line(f"  {sym}", [s for s in taken if s["symbol"] == sym])
        print()


def _sweep(signals, data, target, horizon_ms, args, steps_run):
    prim = [s for s in signals if s["strategy"] == target]
    for s in prim:
        s["_fwd"] = forward_bars(data, s["symbol"], s["ts"], horizon_ms)
    stop_mults = [0.5, 0.75, 1.0, 1.5, 2.0]
    rrs = [1.0, 1.5, 2.0, 2.5, 3.0]
    cost = "no-costs" if args.no_costs else "realistic costs"
    print(f"\n=== EXIT SWEEP: {target} entries fixed, vary stop x rr ===")
    print(f"steps={steps_run}  step={args.step_min}m  entries={len(prim)}  "
          f"non-overlap, {cost}")
    print("cells = total R  (current config = stop 1.00 x rr 2.0)\n")
    print("           " + "".join(f"rr{rr:<6.1f}" for rr in rrs))
    best = None
    for sm in stop_mults:
        cells = []
        for rr in rrs:
            rows = []
            for s in prim:
                base = s["base_stop"]
                if s["direction"] == "long":
                    sl, tp = s["entry"] - sm * base, s["entry"] + rr * sm * base
                else:
                    sl, tp = s["entry"] + sm * base, s["entry"] - rr * sm * base
                res, r, rt = resolve_costed(s, sl, tp, s["_fwd"], args.no_costs)
                rows.append({**s, "result": res, "r": r, "resolve_ts": rt})
            taken = apply_non_overlap(rows)
            n, w, wr, totr, pf = stats(taken)
            cells.append(f"{totr:>+7.1f}")
            if best is None or totr > best[0]:
                best = (totr, sm, rr, wr, pf, n)
        print(f"stop{sm:<5.2f} " + "  ".join(cells))
    print()
    totr, sm, rr, wr, pf, n = best
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"best cell: stop x{sm} rr {rr}  ->  R {totr:+.1f}  WR {wr:.1f}%  PF {pfs}  (n={n})")
    print("if every cell is negative, the ENTRIES have no edge — exits can't save them.")


if __name__ == "__main__":
    asyncio.run(main())
