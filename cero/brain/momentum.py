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

import os
import sqlite3
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


# ──────────────────────────────────────────────────────────────────────
# Paper portfolio book — stateful, persisted. Shared by the daily script
# (scripts/momentum_paper.py) and the in-process worker (momentum engine).
# ──────────────────────────────────────────────────────────────────────

_DAY_MS = 86_400_000


class MomentumBook:
    """Stateful long/short paper book. Call `update(closes, now_ms)` once per
    day: it marks the held book to market, and if a rebalance is due (every
    cfg.rebalance_days) trades the *difference* to the new target. All state
    (equity, positions, trade log) persists to a sqlite file. No real orders."""

    def __init__(self, cfg: MomentumConfig, db_path: str = "data/momentum_paper.db",
                 start_equity: float = 10_000.0, cost: float = 0.001) -> None:
        self.cfg = cfg
        self.db_path = db_path
        self.start_equity = start_equity
        self.cost = cost
        con = sqlite3.connect(self.db_path)
        self._ensure(con)
        con.close()

    @staticmethod
    def _ensure(con) -> None:
        con.execute("CREATE TABLE IF NOT EXISTS mom_state (id INTEGER PRIMARY KEY, equity REAL, last_rebalance INTEGER, start_equity REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS mom_positions (symbol TEXT PRIMARY KEY, size REAL, last_price REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS mom_trades (ts INTEGER, symbol TEXT, side TEXT, qty REAL, price REAL, cost REAL)")
        con.commit()

    def update(self, closes: dict[str, list[float]], now_ms: int, do_rebalance: bool = True) -> dict:
        prices = {s: c[-1] for s, c in closes.items() if c}
        con = sqlite3.connect(self.db_path)
        self._ensure(con)
        st = con.execute("SELECT equity, last_rebalance, start_equity FROM mom_state WHERE id=1").fetchone()
        equity, last_reb, start_eq = st if st else (self.start_equity, 0, self.start_equity)
        positions = {s: (sz, lp) for s, sz, lp in con.execute("SELECT symbol, size, last_price FROM mom_positions")}

        # 1. mark to market — equity moves by the P&L of held positions since last seen
        day_pnl = sum(sz * (prices[s] - lp) for s, (sz, lp) in positions.items() if s in prices)
        equity += day_pnl
        positions = {s: (sz, prices.get(s, lp)) for s, (sz, lp) in positions.items()}

        rebalanced = False
        due = (now_ms - last_reb) >= self.cfg.rebalance_days * _DAY_MS
        if do_rebalance and due:
            w = target_weights(closes, self.cfg)
            if w:
                target = {s: w[s] * equity / prices[s] for s in w if s in prices}
                cost_tot, new_pos = 0.0, {}
                for s in set(positions) | set(target):
                    cur = positions.get(s, (0.0, prices.get(s, 0.0)))[0]
                    tgt = target.get(s, 0.0)
                    if abs(tgt - cur) > 1e-12 and s in prices:
                        qty = tgt - cur
                        c = abs(qty) * prices[s] * self.cost
                        cost_tot += c
                        con.execute("INSERT INTO mom_trades VALUES (?,?,?,?,?,?)",
                                    (now_ms, s, "buy" if qty > 0 else "sell", qty, prices[s], c))
                    if abs(tgt) > 1e-12:
                        new_pos[s] = (tgt, prices[s])
                equity -= cost_tot
                positions = new_pos
                last_reb = now_ms
                rebalanced = True

        con.execute("INSERT OR REPLACE INTO mom_state (id, equity, last_rebalance, start_equity) VALUES (1,?,?,?)",
                    (equity, last_reb, start_eq))
        con.execute("DELETE FROM mom_positions")
        con.executemany("INSERT INTO mom_positions VALUES (?,?,?)",
                        [(s, sz, lp) for s, (sz, lp) in positions.items()])
        con.commit()
        con.close()

        score = momentum_score(closes, self.cfg.lookbacks)
        longs = sorted((s for s, (sz, _) in positions.items() if sz > 0), key=lambda s: -score.get(s, 0))
        shorts = sorted((s for s, (sz, _) in positions.items() if sz < 0), key=lambda s: score.get(s, 0))
        return {"equity": equity, "start_equity": start_eq, "day_pnl": day_pnl,
                "rebalanced": rebalanced, "last_rebalance": last_reb,
                "longs": longs, "shorts": shorts, "n_priced": len(prices)}


def read_book(db_path: str = "data/momentum_paper.db") -> dict:
    """Read-only snapshot of the paper book for UI / Telegram. No writes, no
    network — safe to call while the engine is running. Returns {} if no book."""
    if not os.path.exists(db_path):
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        st = con.execute("SELECT equity, last_rebalance, start_equity FROM mom_state WHERE id=1").fetchone()
        if not st:
            return {}
        equity, last_reb, start_eq = st
        pos = con.execute("SELECT symbol, size, last_price FROM mom_positions").fetchall()
        n_trades = con.execute("SELECT COUNT(*) FROM mom_trades").fetchone()[0]
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    return {
        "equity": equity, "start_equity": start_eq, "last_rebalance": last_reb,
        "n_trades": n_trades,
        "longs": sorted(s for s, sz, _ in pos if sz > 0),
        "shorts": sorted(s for s, sz, _ in pos if sz < 0),
        "positions": {s: (sz, lp) for s, sz, lp in pos},
    }


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
