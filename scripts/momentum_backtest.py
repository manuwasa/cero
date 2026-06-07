"""Backtest the EXACT long/short momentum rule we'd run live — to lock params.

signal_lab.py proved the *family* works (single lookback, daily rebalance). This
tests the *practical* version we'd actually trade:
  - ENSEMBLE of lookbacks (average cross-sectional momentum rank over several L),
    so we don't depend on one cherry-picked lookback,
  - long top tercile / short bottom tercile, equal-weight, dollar-neutral,
  - rebalanced every R days (weekly-ish, not daily — less churn/cost),
  - net of a per-trade cost on rebalance turnover.

If this holds (Sharpe well above 0, positive in both period-halves) at a weekly
rebalance, we lock these params and build the live engine around them.

Usage:
    uv run python scripts/momentum_backtest.py --db data/cero_research_big.db
    uv run python scripts/momentum_backtest.py --lookbacks 20,30,60 --frac 0.3
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

import numpy as np


def load(db: str):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT symbol, open_time, close FROM candles WHERE timeframe='1d' ORDER BY open_time"
    ).fetchall()
    con.close()
    syms = sorted({r[0] for r in rows})
    times = sorted({r[1] for r in rows})
    tidx = {t: i for i, t in enumerate(times)}
    sidx = {s: i for i, s in enumerate(syms)}
    P = np.full((len(times), len(syms)), np.nan)
    for s, t, c in rows:
        P[tidx[t], sidx[s]] = c
    return syms, np.array(times), P


def pct_rank_rows(M: np.ndarray) -> np.ndarray:
    """Per row, percentile-rank (0..1) of each finite value among that row's finite values."""
    R = np.full_like(M, np.nan)
    for t in range(M.shape[0]):
        idx = np.where(np.isfinite(M[t]))[0]
        if len(idx) < 6:
            continue
        order = np.argsort(np.argsort(M[t][idx]))
        R[t, idx] = order / (len(idx) - 1)
    return R


def metrics(daily: np.ndarray):
    d = daily[np.isfinite(daily)]
    if len(d) < 30 or d.std() == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    ann = d.mean() * 365 * 100
    sh = d.mean() / d.std() * np.sqrt(365)
    curve = np.cumprod(1 + d)
    dd = (curve / np.maximum.accumulate(curve) - 1).min() * 100
    h = len(d) // 2

    def s(x):
        return x.mean() / x.std() * np.sqrt(365) if (len(x) > 10 and x.std() > 0) else 0.0

    return ann, sh, dd, s(d[:h]), s(d[h:])


def backtest(P, lookbacks, frac, rebalance, cost):
    T, S = P.shape
    with np.errstate(invalid="ignore"):
        ret_next = np.full((T, S), np.nan)
        ret_next[:-1] = P[1:] / P[:-1] - 1
        # ensemble cross-sectional momentum score = mean percentile-rank over lookbacks
        stack = []
        for L in lookbacks:
            mom = np.full((T, S), np.nan)
            mom[L:] = P[L:] / P[:-L] - 1
            stack.append(pct_rank_rows(mom))
        score = np.nanmean(np.stack(stack), axis=0)   # (T,S), 0..1, nan where insufficient

        W = np.zeros((T, S))
        w_prev = np.zeros(S)
        turnover = np.zeros(T)
        start = max(lookbacks) + 1
        for t in range(T):
            if t < start:
                W[t] = w_prev
                continue
            if (t - start) % rebalance == 0:
                sc = score[t]
                valid = np.where(np.isfinite(sc))[0]
                if len(valid) >= 6:
                    k = max(1, int(len(valid) * frac))
                    order = valid[np.argsort(sc[valid])]
                    w = np.zeros(S)
                    w[order[-k:]] = 1.0 / k          # long top frac
                    w[order[:k]] = -1.0 / k          # short bottom frac
                    turnover[t] = np.abs(w - w_prev).sum()
                    w_prev = w
            W[t] = w_prev
        daily = np.nansum(W * ret_next, axis=1) - turnover * cost
        return metrics(daily)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_research_big.db")
    ap.add_argument("--lookbacks", default="20,30,60")
    ap.add_argument("--frac", type=float, default=0.3, help="long/short fraction of universe each side")
    ap.add_argument("--cost", type=float, default=0.001)
    args = ap.parse_args()

    syms, times, P = load(args.db)
    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    d0 = datetime.fromtimestamp(times[0] / 1000, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(times[-1] / 1000, tz=timezone.utc).date()
    print(f"\n=== Long/short momentum — exact rule === {len(syms)} symbols ({d0} -> {d1})")
    print(f"ensemble lookbacks {lookbacks}, long/short top+bottom {args.frac:.0%}, "
          f"cost {args.cost*100:.2f}%/side\n")
    print(f"{'rebalance':<12}{'ann%':>8}{'Sharpe':>8}{'maxDD%':>8}{'H1 Sh':>7}{'H2 Sh':>7}")
    print("-" * 50)
    for R in (1, 5, 7, 14):
        ann, sh, dd, h1, h2 = backtest(P, lookbacks, args.frac, R, args.cost)
        tag = "  <- weekly" if R == 7 else ""
        print(f"every {R:>2}d {' ':<3}{ann:>8.1f}{sh:>8.2f}{dd:>8.1f}{h1:>7.2f}{h2:>7.2f}{tag}")
    print("\nLock the rule if the weekly (every 7d) row is solidly positive in both halves.")


if __name__ == "__main__":
    main()
