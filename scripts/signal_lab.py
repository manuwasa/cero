"""Strategy search on historical daily data — hunt for edge, fast, no waiting.

Tests the strategy families that actually have academic support, across a basket
of symbols over ~2 years (multiple regimes), net of costs, and benchmarks every
one against just buying and holding. If nothing beats buy-and-hold on a
risk-adjusted basis, that's the honest answer — and far better learned here in
seconds than over weeks of paper trading.

Families tested (on daily bars):
  buy_hold        equal-weight basket, held (the bar everything must beat)
  TSMOM           time-series momentum: long if past L-day return > 0 else short
  TSMOM_long      long-only variant (long when up, else flat — no shorting)
  XSMOM           cross-sectional momentum: long top third / short bottom third
                  ranked by past L-day return (market-neutral)
  XS_reversal     the opposite (long losers / short winners) — tests reversion

Metric = annualized return and **Sharpe** (return per unit of risk). Raw return
in crypto is mostly beta; Sharpe is what tells you if there's real skill. Shown
net of a per-trade cost. The H1/H2 columns are the Sharpe of the first vs second
half of the period — a strategy positive in BOTH halves is far more credible
than one carried by a single lucky stretch (the key overfit check).

Caveats baked in: (1) the basket is *surviving* liquid symbols — survivorship
bias flatters momentum; (2) one ~2yr window; (3) shorting frictions + funding on
the short legs are NOT modeled. So a standout is a *candidate* to validate
further, not a guarantee.

Usage:
    uv run python scripts/signal_lab.py --db data/cero_research.db
    uv run python scripts/signal_lab.py --cost 0.001
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sqlite3

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


def stats(daily: np.ndarray):
    """Annualized return %, Sharpe, max drawdown % from a daily-return series."""
    d = daily[np.isfinite(daily)]
    if len(d) < 30 or d.std() == 0:
        return 0.0, 0.0, 0.0
    ann = d.mean() * 365 * 100
    sharpe = (d.mean() / d.std()) * np.sqrt(365)
    curve = np.cumprod(1 + d)
    dd = (curve / np.maximum.accumulate(curve) - 1).min() * 100
    return ann, sharpe, dd


def half_sharpe(daily: np.ndarray):
    d = daily[np.isfinite(daily)]
    h = len(d) // 2

    def sh(x):
        return (x.mean() / x.std() * np.sqrt(365)) if (len(x) > 10 and x.std() > 0) else 0.0

    return sh(d[:h]), sh(d[h:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_research.db")
    ap.add_argument("--cost", type=float, default=0.001,
                    help="one-way trade cost as fraction of notional (0.001 = 0.1 pct)")
    args = ap.parse_args()

    syms, times, P = load(args.db)
    T, S = P.shape
    d0 = datetime.fromtimestamp(times[0] / 1000, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(times[-1] / 1000, tz=timezone.utc).date()
    print(f"\n=== Strategy search === {len(syms)} symbols, {T} days ({d0} -> {d1})")
    print(f"cost: {args.cost*100:.2f}% one-way   (H1/H2 = first-half / second-half Sharpe)\n")

    series: list[tuple[str, np.ndarray]] = []
    with np.errstate(invalid="ignore"):
        ret_next = np.full((T, S), np.nan)
        ret_next[:-1] = P[1:] / P[:-1] - 1            # ret_next[t] = day t -> t+1

        def apply_cost(daily, pos):
            turn = np.zeros(T)
            turn[1:] = np.nanmean(np.abs(np.diff(pos, axis=0)), axis=1)
            return daily - turn * args.cost

        series.append(("buy_hold_eqw", np.nanmean(ret_next, axis=1)))

        for L in (10, 20, 30, 60, 90):
            mom = np.full((T, S), np.nan)
            mom[L:] = P[L:] / P[:-L] - 1

            pos = np.sign(mom)
            series.append((f"TSMOM L={L}", apply_cost(np.nanmean(pos * ret_next, axis=1), pos)))

            posl = (mom > 0).astype(float)
            posl[np.isnan(mom)] = np.nan
            series.append((f"TSMOM_long L={L}", apply_cost(np.nanmean(posl * ret_next, axis=1), posl)))

            W = np.full((T, S), np.nan)
            Wr = np.full((T, S), np.nan)
            for t in range(L, T):
                m = mom[t]
                valid = np.where(np.isfinite(m))[0]
                if len(valid) < 6:
                    continue
                order = valid[np.argsort(m[valid])]
                k = max(1, len(valid) // 3)
                W[t] = 0.0
                W[t, order[-k:]] = 1.0 / k          # long winners
                W[t, order[:k]] = -1.0 / k          # short losers
                Wr[t] = -W[t]
            series.append((f"XSMOM L={L}", apply_cost(np.nansum(W * ret_next, axis=1), np.nan_to_num(W))))
            series.append((f"XS_reversal L={L}", apply_cost(np.nansum(Wr * ret_next, axis=1), np.nan_to_num(Wr))))

    bh_sharpe = stats(series[0][1])[1]
    print(f"{'strategy':<20}{'ann%':>8}{'Sharpe':>8}{'maxDD%':>8}{'H1 Sh':>7}{'H2 Sh':>7}")
    print("-" * 58)
    rows = []
    for name, d in series:
        ann, sh, dd = stats(d)
        h1, h2 = half_sharpe(d)
        rows.append((name, ann, sh, dd, h1, h2))
        robust = (not name.startswith("buy_hold")) and sh > bh_sharpe + 0.2 and h1 > 0.1 and h2 > 0.1
        beats = (not name.startswith("buy_hold")) and sh > bh_sharpe + 0.2
        flag = "  <== ROBUST" if robust else ("  <- beats hold" if beats else "")
        print(f"{name:<20}{ann:>8.1f}{sh:>8.2f}{dd:>8.1f}{h1:>7.2f}{h2:>7.2f}{flag}")

    print()
    print(f"buy-and-hold Sharpe: {bh_sharpe:.2f}  (the bar to beat)")
    robust = [r for r in rows if not r[0].startswith("buy_hold")
              and r[2] > bh_sharpe + 0.2 and r[4] > 0.1 and r[5] > 0.1]
    robust.sort(key=lambda r: -r[2])
    if robust:
        print("\nROBUST candidates — beat hold AND Sharpe>0 in BOTH halves:")
        for r in robust[:5]:
            print(f"  {r[0]:<16} Sharpe {r[2]:.2f}  (H1 {r[4]:.2f} / H2 {r[5]:.2f})  maxDD {r[3]:.0f}%")
        print("\nThese survive the overfit check. NEXT to trust them with money:")
        print("  1. test on a *different* basket + period (out-of-sample)")
        print("  2. model real shorting cost + funding on the short legs")
        print("  3. build it into Cero, forward paper-test, THEN small real money")
    else:
        print("VERDICT: nothing is both better than hold AND stable across both halves.")


if __name__ == "__main__":
    main()
