"""
Cross-sectional long/short momentum — portfolio target weights.

This is a DIFFERENT shape of strategy from the per-symbol smc_trend brain. Given
recent daily closes for a whole universe of symbols, it ranks them by an ensemble
of momentum lookbacks and returns a target portfolio: long the strongest `frac`,
short the weakest `frac`, equal-weight, dollar-neutral. Pure functions — no I/O.

Validated in scripts/signal_lab.py + scripts/momentum_backtest.py: over ~2 years
across 40+ alts AND two exchanges (Bybit, Binance) it beats buy-and-hold (which
lost money), at ~0.7 Sharpe with the locked config below, positive in both halves.

HONEST framing: the edge is REAL but MODEST and parameter-sensitive (5d rebalance
worked, 7d didn't). Treat the backtest Sharpe as an optimistic ceiling — live will
likely be lower (survivorship bias in the universe; recent-period softness). Use
the ensemble, rebalance ~5d, and validate forward in paper before real money.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MomentumConfig:
    """Locked v1 parameters (see module docstring for validation)."""
    universe: tuple[str, ...]                       # symbols allowed to trade
    lookbacks: tuple[int, ...] = (20, 30, 60)       # days; ensemble (don't use one)
    frac: float = 0.30                              # long top 30% / short bottom 30%
    rebalance_days: int = 5                         # the robust sweet spot
    gross_per_side: float = 1.0                     # long notional = short notional = this x equity


def momentum_score(closes: dict[str, list[float]], lookbacks) -> dict[str, float]:
    """Ensemble cross-sectional momentum score per symbol, in [0, 1].

    `closes[sym]` is that symbol's daily closes, oldest -> newest. For each
    lookback we compute the L-day return, rank symbols cross-sectionally into a
    percentile (0=weakest, 1=strongest), and average those percentiles across
    lookbacks. Symbols without enough history are excluded.
    """
    need = max(lookbacks) + 1
    syms = [s for s, c in closes.items() if c is not None and len(c) >= need]
    if len(syms) < 6:
        return {}
    acc: dict[str, float] = {s: 0.0 for s in syms}
    for L in lookbacks:
        mom = {s: closes[s][-1] / closes[s][-1 - L] - 1.0 for s in syms}
        order = sorted(syms, key=lambda s: mom[s])      # weakest -> strongest
        n = len(order)
        for i, s in enumerate(order):
            acc[s] += i / (n - 1)                        # percentile for this lookback
    return {s: acc[s] / len(lookbacks) for s in syms}


def target_weights(closes: dict[str, list[float]], cfg: MomentumConfig) -> dict[str, float]:
    """Target portfolio weights: +gross/k on each of the top-`frac` symbols,
    -gross/k on each of the bottom-`frac`. Dollar-neutral (sum of longs = -sum of
    shorts). Empty dict if the universe is too small to rank. A weight is a
    fraction of equity: e.g. +0.10 = hold a long worth 10% of equity in that coin.
    """
    score = momentum_score(closes, cfg.lookbacks)
    if len(score) < 6:
        return {}
    ranked = sorted(score, key=score.get)               # weakest -> strongest
    k = max(1, int(len(ranked) * cfg.frac))
    longs, shorts = ranked[-k:], ranked[:k]
    w: dict[str, float] = {}
    for s in longs:
        w[s] = cfg.gross_per_side / k
    for s in shorts:
        w[s] = -cfg.gross_per_side / k
    return w


# ── quick self-test / inspection: show today's target book from a DB ──────
if __name__ == "__main__":
    import sqlite3
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else "data/cero_research_big.db"
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT symbol, open_time, close FROM candles WHERE timeframe='1d' ORDER BY open_time"
    ).fetchall()
    con.close()
    closes: dict[str, list[float]] = {}
    for s, _, c in rows:
        closes.setdefault(s, []).append(c)

    cfg = MomentumConfig(universe=tuple(closes))
    score = momentum_score(closes, cfg.lookbacks)
    w = target_weights(closes, cfg)
    print(f"universe: {len(closes)} symbols   ranked: {len(score)}   "
          f"book: {sum(1 for v in w.values() if v > 0)} long / {sum(1 for v in w.values() if v < 0)} short")
    print("\nTODAY's target book (what it would hold now):")
    longs = sorted((s for s in w if w[s] > 0), key=lambda s: -score[s])
    shorts = sorted((s for s in w if w[s] < 0), key=lambda s: score[s])
    print("  LONG (strongest momentum): " + ", ".join(f"{s.split('/')[0]}({score[s]:.2f})" for s in longs))
    print("  SHORT (weakest momentum):  " + ", ".join(f"{s.split('/')[0]}({score[s]:.2f})" for s in shorts))
