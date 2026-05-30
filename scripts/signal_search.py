"""Predictive-signal screen — does ANYTHING beat chance at calling direction?

The baseline test proved smc_trend's entries are no better than random, and the
fixed 2:1/ATR/24h exit framework loses even cost-free. Before redesigning, this
asks the question underneath all of it: on clean mainnet data, does any simple
feature predict the FORWARD RETURN? (Forward return, not the 2:1 outcome — so the
broken exit structure can't contaminate the answer.)

For each 1h bar it computes momentum + mean-reversion features (using only past
data) and the forward return at 4h/12h/24h, then reports each feature's
Information Coefficient (correlation with forward return) and its top-vs-bottom
decile forward-return spread.

Reading it:
  |IC| < ~0.03 everywhere  -> no exploitable directional signal on this data.
                              Intraday direction on majors is ~efficient here;
                              a redesign needs a different premise (instruments,
                              horizon, or non-directional).
  |IC| > ~0.05, monotone deciles, consistent sign across horizons -> a real lead.
  positive IC on momentum feats -> trend-continuation regime;
  negative IC on rsi/extension  -> mean-reversion regime.

Caveat: 1h stepping with multi-hour forward windows overlaps, so significance is
overstated — but the IC sign/magnitude point estimates are what we screen on.

Usage:
    uv run python scripts/signal_search.py
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np

H1, H4 = 3_600_000, 14_400_000
DEFAULT_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT"]


def load(db, sym, tf):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT open_time,high,low,close FROM candles WHERE symbol=? AND timeframe=? "
        "ORDER BY open_time", (sym, tf)).fetchall()
    con.close()
    a = np.array(rows, dtype=float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]   # ot, high, low, close


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


def trend_arr(c, p=50):
    e = ema(c, p)
    slope = np.empty_like(e)
    slope[0] = 0.0
    slope[1:] = e[1:] - e[:-1]
    t = np.zeros(len(c))
    t[(c > e) & (slope > 0)] = 1.0
    t[(c < e) & (slope < 0)] = -1.0
    return t


def ic(feat, fwd):
    m = np.isfinite(feat) & np.isfinite(fwd)
    if m.sum() < 50 or np.std(feat[m]) == 0:
        return 0.0, 0
    return float(np.corrcoef(feat[m], fwd[m])[0, 1]), int(m.sum())


def decile_spread(feat, fwd):
    m = np.isfinite(feat) & np.isfinite(fwd)
    f, r = feat[m], fwd[m]
    if len(f) < 100:
        return 0.0
    order = np.argsort(f)
    k = len(f) // 10
    bot = r[order[:k]].mean()
    top = r[order[-k:]].mean()
    return (top - bot) * 100   # percentage points of forward return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero_mainnet.db")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--warmup", type=int, default=200, help="1h bars to skip (EMA warmup)")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # accumulate (feature, fwd) pairs pooled across symbols
    feats = {n: [] for n in ("htf_trend", "ret_1h", "ret_4h", "ret_12h",
                              "ext_ema50", "rsi14", "range_pos_24h")}
    fwds = {4: [], 12: [], 24: []}
    htf_all, fwd24_all = [], []

    for sym in symbols:
        ot1, hi1, lo1, c1 = load(args.db, sym, "1h")
        ot4, _, _, c4 = load(args.db, sym, "4h")
        n = len(c1)
        if n < args.warmup + 30:
            continue
        e50 = ema(c1, 50)
        t1 = trend_arr(c1, 50)
        t4 = trend_arr(c4, 50)
        # map 4h trend onto the 1h timeline (latest 4h bar closed by 1h bar's close)
        cutoff = ot1 + H1 - H4
        idx4 = np.searchsorted(ot4, cutoff, side="right") - 1
        t4m = np.where(idx4 >= 0, t4[np.clip(idx4, 0, len(t4) - 1)], 0.0)
        htf = np.where((t1 == 1) & (t4m == 1), 1.0,
                       np.where((t1 == -1) & (t4m == -1), -1.0, 0.0))

        def ret_back(k):
            r = np.full(n, np.nan)
            r[k:] = c1[k:] / c1[:-k] - 1
            return r

        # 24h rolling range position (last 24 1h bars)
        rp = np.full(n, np.nan)
        for i in range(24, n):
            lo = lo1[i - 24:i].min(); hi = hi1[i - 24:i].max()
            rp[i] = (c1[i] - lo) / (hi - lo) if hi > lo else 0.5

        feat_arrays = {
            "htf_trend": htf,
            "ret_1h": ret_back(1),
            "ret_4h": ret_back(4),
            "ret_12h": ret_back(12),
            "ext_ema50": (c1 - e50) / e50,
            "rsi14": rsi(c1, 14),
            "range_pos_24h": rp,
        }

        def fwd_ret(h):
            r = np.full(n, np.nan)
            r[:-h] = c1[h:] / c1[:-h] - 1
            return r
        fr = {4: fwd_ret(4), 12: fwd_ret(12), 24: fwd_ret(24)}

        w = slice(args.warmup, n)
        for name, arr in feat_arrays.items():
            # store pairs against the 24h horizon (primary)
            feats[name].append((arr[w], {h: fr[h][w] for h in (4, 12, 24)}))
        htf_all.append(htf[w]); fwd24_all.append(fr[24][w])

    print(f"\n=== Predictive-signal screen (mainnet 1h; IC = corr with forward return) ===")
    print(f"symbols={len(symbols)}   (caveat: overlapping windows -> significance overstated)\n")
    print(f"{'feature':<15}{'IC@4h':>9}{'IC@12h':>9}{'IC@24h':>9}{'decile@24h':>13}")
    print("-" * 56)
    best = (0.0, "")
    for name in feats:
        # pool across symbols
        fcat = np.concatenate([p[0] for p in feats[name]])
        ics = {}
        for h in (4, 12, 24):
            fwdcat = np.concatenate([p[1][h] for p in feats[name]])
            ics[h], _ = ic(fcat, fwdcat)
        ds = decile_spread(fcat, np.concatenate([p[1][24] for p in feats[name]]))
        for h in (4, 12, 24):
            if abs(ics[h]) > abs(best[0]):
                best = (ics[h], f"{name}@{h}h")
        print(f"{name:<15}{ics[4]:>+9.3f}{ics[12]:>+9.3f}{ics[24]:>+9.3f}{ds:>+12.2f}%")

    # directional hit-rate for the HTF trend signal
    h_all = np.concatenate(htf_all); f_all = np.concatenate(fwd24_all)
    m = (h_all != 0) & np.isfinite(f_all)
    hit = np.mean(np.sign(f_all[m]) == np.sign(h_all[m])) * 100 if m.sum() else 0.0
    print(f"\nHTF-trend directional hit-rate @24h: {hit:.1f}%  (n={int(m.sum())}, 50% = no edge)")

    print()
    if abs(best[0]) < 0.03:
        print(f"VERDICT: strongest signal is {best[1]} at IC {best[0]:+.3f} -- negligible.")
        print("         No feature predicts direction better than chance on this data.")
        print("         A redesign needs a different premise (instruments / horizon /")
        print("         non-directional), not more criteria of this kind.")
    else:
        print(f"VERDICT: best signal {best[1]} at IC {best[0]:+.3f} -- a real lead to build on.")
        print("         Sign tells the regime: +momentum feats = trend; -rsi/extension = mean-revert.")


if __name__ == "__main__":
    main()
