"""Prototype: is the mean-reversion edge actually TRADEABLE after costs?

signal_search.py showed a consistent mean-reversion signal (fade extension/RSI;
trend predicts nothing). This turns that correlation into a concrete rule and
backtests it on mainnet to see if it survives costs — the make-or-break question
for a high-WR/low-RR style.

Rule (prototype, NOT a committed strategy — no docs/CRITERIA.md change):
  regime filter : Kaufman efficiency ratio over 24h < ER_MAX  (trade only in chop)
  short setup   : RSI(1h) > RSI_HI and price > EMA50 by >= MIN_EXT * ATR
  long setup    : RSI(1h) < RSI_LO and price < EMA50 by >= MIN_EXT * ATR
  entry         : 1h close;  TP = EMA50 (revert to mean);  SL = entry +/- SL_ATR * ATR
  resolve       : forward 5m candles, 24h horizon, SL-first same-bar = loss

Reported across 3 cost scenarios because MR's thin edge is cost-sensitive:
  ideal (0/0) | maker/limit (0.01% slip + 0.02% fee) | taker (0.10%/0.06%, pessimistic)

Reuses scripts/backtest_mainnet.py for data + resolution so logic stays single-sourced.

Usage:
    uv run python scripts/proto_meanrev.py
    uv run python scripts/proto_meanrev.py --er-max 0.25 --rsi-hi 70 --rsi-lo 30
"""
from __future__ import annotations

import argparse
import bisect
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_mainnet as bt  # noqa: E402

from cero.brain.indicators import atr as atr_fn  # noqa: E402

H1 = 3_600_000
COST_SCENARIOS = [("ideal", 0.0, 0.0), ("maker", 0.01, 0.02), ("taker", 0.10, 0.06)]


def ema(x, p):
    a = 2.0 / (p + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(c, p=14):
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), -np.clip(d, None, 0)
    ru, rd = np.empty_like(c), np.empty_like(c)
    ru[0], rd[0] = up[0], dn[0]
    for i in range(1, len(c)):
        ru[i] = (ru[i - 1] * (p - 1) + up[i]) / p
        rd[i] = (rd[i - 1] * (p - 1) + dn[i]) / p
    return 100 - 100 / (1 + ru / np.where(rd == 0, 1e-9, rd))


def efficiency_ratio(c, n):
    er = np.full(len(c), np.nan)
    ad = np.abs(np.diff(c, prepend=c[0]))
    for i in range(n, len(c)):
        denom = ad[i - n + 1:i + 1].sum()
        er[i] = abs(c[i] - c[i - n]) / denom if denom > 0 else 0.0
    return er


def cost_r(entry, stop_dist, slip, fee):
    if stop_dist <= 0:
        return 0.0
    return (entry * (slip + fee) / 100 * 2) / stop_dist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_mainnet.db")
    ap.add_argument("--symbols", default=",".join(bt._ROUND_STEPS))
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--horizon-hours", type=int, default=24)
    ap.add_argument("--er-max", type=float, default=0.30)
    ap.add_argument("--rsi-hi", type=float, default=65.0)
    ap.add_argument("--rsi-lo", type=float, default=35.0)
    ap.add_argument("--min-ext", type=float, default=0.5, help="min extension from EMA in ATRs")
    ap.add_argument("--sl-atr", type=float, default=1.0)
    ap.add_argument("--no-regime", action="store_true", help="disable ER regime filter")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    horizon_ms = args.horizon_hours * 3600_000
    data = bt.load_candles(args.db, symbols)

    raw = []  # signal dicts (entry/sl/tp/direction/symbol/ts), unresolved
    n_long = n_short = 0
    for sym in symbols:
        ot1, hi1, lo1, c1 = bt_load_1h(data, sym)
        if len(c1) < args.warmup + 30:
            continue
        e50 = ema(c1, 50)
        r = rsi(c1, 14)
        a = np.asarray(atr_fn(list(hi1), list(lo1), list(c1), 14), dtype=float)
        er = efficiency_ratio(c1, 24)
        for i in range(args.warmup, len(c1)):
            atr_i = a[i]
            if not np.isfinite(atr_i) or atr_i <= 0:
                continue
            if not args.no_regime and (not np.isfinite(er[i]) or er[i] >= args.er_max):
                continue
            ext = (c1[i] - e50[i]) / atr_i   # how many ATRs above (+) / below (-) the mean
            direction = None
            if r[i] >= args.rsi_hi and ext >= args.min_ext:
                direction = "short"
            elif r[i] <= args.rsi_lo and ext <= -args.min_ext:
                direction = "long"
            if direction is None:
                continue
            entry = c1[i]
            tp = e50[i]                                   # revert to the mean
            sl = entry + args.sl_atr * atr_i if direction == "short" else entry - args.sl_atr * atr_i
            dtime = ot1[i] + H1
            raw.append({"symbol": sym, "ts": dtime, "direction": direction,
                        "entry": entry, "sl": sl, "tp": tp})
            n_long += direction == "long"
            n_short += direction == "short"

    # precompute forward bars + base outcome (cost-free) once
    for s in raw:
        fwd = bt.forward_bars(data, s["symbol"], s["ts"] - 1, horizon_ms)
        res, rmult, bars = bt.resolve(s["direction"], s["entry"], s["sl"], s["tp"], fwd)
        s.update(_res=res, _r=rmult, _resolve_ts=s["ts"] + bars * 300_000,
                 _stop=abs(s["entry"] - s["sl"]))

    print(f"\n=== Prototype mean-reversion (fade extremes{' + ER regime filter' if not args.no_regime else ', NO regime filter'}) ===")
    print(f"params: RSI {args.rsi_lo:.0f}/{args.rsi_hi:.0f}, ER<{args.er_max}, "
          f"ext>={args.min_ext} ATR, SL {args.sl_atr} ATR, TP=EMA50, horizon {args.horizon_hours}h")
    print(f"signals: {len(raw)}  ({n_long} long / {n_short} short)\n")
    if not raw:
        print("no signals — loosen thresholds."); return

    print(f"{'cost scenario':<18}{'dec':>5}{'WR':>8}{'totR':>9}{'PF':>7}{'avgRR':>7}")
    print("-" * 54)
    for name, slip, fee in COST_SCENARIOS:
        rows = []
        for s in raw:
            rr = s["_r"]
            if s["_res"] in ("win", "loss"):
                rr = s["_r"] - cost_r(s["entry"], s["_stop"], slip, fee)
            rows.append({**s, "result": s["_res"], "r": rr, "resolve_ts": s["_resolve_ts"]})
        taken = bt.apply_non_overlap(rows)
        n, w, wr, totr, pf = bt.stats(taken)
        # avg realized RR on wins (target/stop), informational
        wins = [t for t in taken if t["result"] == "win"]
        avgrr = np.mean([abs(t["entry"] - t["tp"]) / abs(t["entry"] - t["sl"]) for t in wins]) if wins else 0.0
        pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{name:<18}{n:>5}{wr:>7.1f}%{totr:>+9.1f}{pfs:>7}{avgrr:>7.2f}")

    # per-symbol at maker costs
    print("\nper symbol (maker costs):")
    for sym in symbols:
        rows = []
        for s in raw:
            if s["symbol"] != sym:
                continue
            rr = s["_r"]
            if s["_res"] in ("win", "loss"):
                rr = s["_r"] - cost_r(s["entry"], s["_stop"], 0.01, 0.02)
            rows.append({**s, "result": s["_res"], "r": rr, "resolve_ts": s["_resolve_ts"]})
        taken = bt.apply_non_overlap(rows)
        if not taken:
            continue
        n, w, wr, totr, pf = bt.stats(taken)
        pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"  {sym:<18}{n:>4} dec  WR {wr:>5.1f}%  R {totr:>+7.1f}  PF {pfs}")

    # ── time-exit variant: harvest the close-to-close reversion the IC measured ──
    print("\n--- time-exit variant (hold N hours, no stop/target; matches what IC measured) ---")
    print("return = % of notional per trade, net of round-trip cost; non-overlap per symbol")
    print(f"{'hold':>5} {'cost':<7}{'n':>5}{'WR':>8}{'mean/trade':>12}{'total':>9}")
    print("-" * 46)
    for hold_h in (8, 12, 24):
        hold_ms = hold_h * 3600_000
        trades = []
        for s in raw:
            c5, ot5 = data[s["symbol"]]["5m"]
            j = bisect.bisect_left(ot5, s["ts"] + hold_ms)
            if j >= len(ot5):
                continue
            ex = c5[j].close
            rret = (ex - s["entry"]) / s["entry"] if s["direction"] == "long" else (s["entry"] - ex) / s["entry"]
            trades.append({"symbol": s["symbol"], "ts": s["ts"],
                           "resolve_ts": s["ts"] + hold_ms, "rret": rret})
        bysym = defaultdict(list)
        for t in trades:
            bysym[t["symbol"]].append(t)
        taken = []
        for lst in bysym.values():
            lst.sort(key=lambda x: x["ts"])
            free = 0
            for t in lst:
                if t["ts"] >= free:
                    taken.append(t)
                    free = t["resolve_ts"]
        for cname, slip, fee in COST_SCENARIOS:
            ctot = (slip + fee) * 2 / 100
            nets = [t["rret"] - ctot for t in taken]
            n = len(nets)
            wr = 100 * sum(1 for x in nets if x > 0) / n if n else 0.0
            print(f"{hold_h:>4}h {cname:<7}{n:>5}{wr:>7.1f}%{np.mean(nets) * 100:>+11.2f}%{sum(nets) * 100:>+8.1f}%")

    print("\nReminder: one ~45d range-bound window; the ER filter is what guards against")
    print("trend regimes. Re-test on a trending window before trusting (regime risk).")


def bt_load_1h(data, sym):
    cands, _ = data[sym]["1h"]
    ot = np.array([c.open_time for c in cands], dtype=float)
    hi = np.array([c.high for c in cands], dtype=float)
    lo = np.array([c.low for c in cands], dtype=float)
    cl = np.array([c.close for c in cands], dtype=float)
    return ot, hi, lo, cl


if __name__ == "__main__":
    main()
