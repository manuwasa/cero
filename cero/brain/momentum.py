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
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MomentumConfig:
    """Locked v1 signal parameters + the risk overlay.

    The *signal* (which coins to long/short) is the validated v1: ensemble
    lookbacks, 5d rebalance, top/bottom `frac`. The *risk overlay* below decides
    HOW MUCH of each — it's what turns the raw signal into a book that survives a
    crash. These default ON because this is built for real money; set
    weighting='equal', target_vol=0, and the halts to 0 to recover raw v1."""
    universe: tuple[str, ...]                       # symbols allowed to trade
    lookbacks: tuple[int, ...] = (20, 30, 60)       # days; ensemble (don't use one)
    frac: float = 0.30                              # long top 30% / short bottom 30%
    rebalance_days: int = 5                         # the robust sweet spot
    gross_per_side: float = 1.0                     # base long notional = short notional = this x equity

    # ── risk overlay ──────────────────────────────────────────────────
    weighting: str = "inverse_vol"                  # 'inverse_vol' (risk parity) | 'equal'
    vol_window: int = 30                            # days of returns for vol estimates
    target_vol: float = 0.25                        # target annualized book vol; 0 = off
    max_gross_per_side: float = 1.0                 # leverage cap — vol targeting never exceeds this
    daily_loss_halt_pct: float = 8.0               # flatten+halt if a cycle loses >= this % ; 0 = off
    drawdown_halt_pct: float = 15.0                 # flatten+halt if >= this % below peak equity; 0 = off


_TRADING_DAYS = 365  # crypto trades every day — annualize daily vol by sqrt(365)


def _daily_returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]


def realized_vol(closes: list[float], window: int) -> float:
    """Sample stdev of the last `window` daily returns (0 if too little history)."""
    rets = _daily_returns(closes[-(window + 1):])
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    return (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5


def portfolio_vol(weights: dict[str, float], closes: dict[str, list[float]],
                  window: int) -> float:
    """Annualized vol of the *book* implied by `weights`, using the realized
    co-movement of its members over the last `window` days. This captures the
    long/short cancellation (a neutral book is far less volatile than its legs),
    so it's the right quantity to vol-target on. 0 if not enough aligned data."""
    series = {s: _daily_returns(closes[s][-(window + 1):]) for s in weights if closes.get(s)}
    series = {s: r for s, r in series.items() if len(r) >= 2}
    if not series:
        return 0.0
    n = min(len(r) for r in series.values())
    if n < 2:
        return 0.0
    port = [sum(weights[s] * series[s][-n + i] for s in series) for i in range(n)]
    mean = sum(port) / n
    daily = (sum((p - mean) ** 2 for p in port) / (n - 1)) ** 0.5
    return daily * (_TRADING_DAYS ** 0.5)


def circuit_breach(equity: float, peak_equity: float, day_pnl: float,
                   cfg: MomentumConfig) -> str | None:
    """Return a human reason if a halt condition is hit, else None. Checked every
    cycle: a drawdown from the high-water mark, or a single-cycle loss."""
    if cfg.drawdown_halt_pct and peak_equity > 0:
        dd = equity / peak_equity - 1.0
        if dd <= -cfg.drawdown_halt_pct / 100.0:
            return f"drawdown {dd * 100:.1f}% ≤ -{cfg.drawdown_halt_pct:.0f}% from peak {peak_equity:.0f}"
    prev = equity - day_pnl
    if cfg.daily_loss_halt_pct and prev > 0:
        day_ret = day_pnl / prev
        if day_ret <= -cfg.daily_loss_halt_pct / 100.0:
            return f"cycle loss {day_ret * 100:.1f}% ≤ -{cfg.daily_loss_halt_pct:.0f}%"
    return None


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


def _leg_weights(syms: list[str], sign: float, gross: float,
                 closes: dict[str, list[float]], cfg: MomentumConfig) -> dict[str, float]:
    """Split `gross` across one leg. inverse_vol = risk parity (∝ 1/vol, so a
    wild micro-cap gets a SMALLER position than a calm blue chip); equal = the
    raw v1. Falls back to equal if vols are unavailable."""
    if not syms:
        return {}
    if cfg.weighting == "inverse_vol":
        inv = {s: (1.0 / v if (v := realized_vol(closes.get(s, []), cfg.vol_window)) > 1e-9 else 0.0)
               for s in syms}
        tot = sum(inv.values())
        if tot > 0:
            return {s: sign * gross * inv[s] / tot for s in syms}
    return {s: sign * gross / len(syms) for s in syms}     # equal-weight fallback / mode


def target_weights(closes: dict[str, list[float]], cfg: MomentumConfig) -> dict[str, float]:
    """Risk-managed target weights. Long the top-`frac`, short the bottom-`frac`,
    dollar-neutral. Each leg is sized by `cfg.weighting` (inverse-vol risk parity
    by default), then the whole book is scaled toward `cfg.target_vol` and capped
    at `cfg.max_gross_per_side` so it can de-lever in a storm but never over-lever.
    A weight is a fraction of equity (+0.10 = a long worth 10% of equity). Empty
    dict if the universe is too small to rank."""
    score = momentum_score(closes, cfg.lookbacks)
    if len(score) < 6:
        return {}
    ranked = sorted(score, key=score.get)               # weakest -> strongest
    k = max(1, int(len(ranked) * cfg.frac))
    longs, shorts = ranked[-k:], ranked[:k]

    w: dict[str, float] = {}
    w.update(_leg_weights(longs, +1.0, cfg.gross_per_side, closes, cfg))
    w.update(_leg_weights(shorts, -1.0, cfg.gross_per_side, closes, cfg))

    # volatility targeting: scale the whole book toward a constant risk level.
    # Capped at max_gross_per_side / gross_per_side so it only ever de-levers
    # below the base unless you explicitly raise the cap (anti-blowup).
    if cfg.target_vol and cfg.gross_per_side > 0:
        pv = portfolio_vol(w, closes, cfg.vol_window)
        if pv > 1e-9:
            scale = min(cfg.target_vol / pv, cfg.max_gross_per_side / cfg.gross_per_side)
            w = {s: x * scale for s, x in w.items()}
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
        con.execute("CREATE TABLE IF NOT EXISTS mom_state (id INTEGER PRIMARY KEY, equity REAL, last_rebalance INTEGER, start_equity REAL, peak_equity REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS mom_positions (symbol TEXT PRIMARY KEY, size REAL, last_price REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS mom_trades (ts INTEGER, symbol TEXT, side TEXT, qty REAL, price REAL, cost REAL)")
        # migrate older books that predate the high-water-mark column
        cols = {r[1] for r in con.execute("PRAGMA table_info(mom_state)")}
        if "peak_equity" not in cols:
            con.execute("ALTER TABLE mom_state ADD COLUMN peak_equity REAL")
        con.commit()

    def _apply_target(self, con, positions: dict, target: dict, prices: dict, now_ms: int) -> float:
        """Trade the difference from current positions to `target` (coin-unit
        sizes); log each fill; return total cost charged. target={} flattens."""
        cost_tot = 0.0
        for s in set(positions) | set(target):
            cur = positions.get(s, (0.0, 0.0))[0]
            tgt = target.get(s, 0.0)
            if abs(tgt - cur) > 1e-12 and s in prices:
                qty = tgt - cur
                c = abs(qty) * prices[s] * self.cost
                cost_tot += c
                con.execute("INSERT INTO mom_trades VALUES (?,?,?,?,?,?)",
                            (now_ms, s, "buy" if qty > 0 else "sell", qty, prices[s], c))
        return cost_tot

    def update(self, closes: dict[str, list[float]], now_ms: int,
               do_rebalance: bool = True, external_halt: bool = False) -> dict:
        prices = {s: c[-1] for s, c in closes.items() if c}
        con = sqlite3.connect(self.db_path)
        self._ensure(con)
        st = con.execute("SELECT equity, last_rebalance, start_equity, peak_equity FROM mom_state WHERE id=1").fetchone()
        if st:
            equity, last_reb, start_eq, peak = st
        else:
            equity, last_reb, start_eq, peak = self.start_equity, 0, self.start_equity, self.start_equity
        peak = peak or self.start_equity
        positions = {s: (sz, lp) for s, sz, lp in con.execute("SELECT symbol, size, last_price FROM mom_positions")}

        # 1. mark to market — equity moves by the P&L of held positions since last seen
        day_pnl = sum(sz * (prices[s] - lp) for s, (sz, lp) in positions.items() if s in prices)
        equity += day_pnl
        positions = {s: (sz, prices.get(s, lp)) for s, (sz, lp) in positions.items()}
        peak = max(peak, equity)

        # 2. RISK GATE (checked every cycle, before any rebalance):
        #    external kill switch (/trip) OR a circuit-breaker breach → FLATTEN
        #    the whole book to cash and do NOT re-enter. Loss containment first.
        if external_halt:
            halt_reason: str | None = "kill switch (/trip)"
        else:
            halt_reason = circuit_breach(equity, peak, day_pnl, self.cfg)

        rebalanced = flattened = False
        if halt_reason:
            if any(abs(sz) > 1e-12 for sz, _ in positions.values()):
                equity -= self._apply_target(con, positions, {}, prices, now_ms)  # sell everything
                positions = {}
                flattened = True
        elif do_rebalance and (now_ms - last_reb) >= self.cfg.rebalance_days * _DAY_MS:
            w = target_weights(closes, self.cfg)
            if w:
                target = {s: w[s] * equity / prices[s] for s in w if s in prices}
                equity -= self._apply_target(con, positions, target, prices, now_ms)
                positions = {s: (sz, prices[s]) for s, sz in target.items()
                             if abs(sz) > 1e-12 and s in prices}
                last_reb = now_ms
                rebalanced = True

        con.execute("INSERT OR REPLACE INTO mom_state (id, equity, last_rebalance, start_equity, peak_equity) VALUES (1,?,?,?,?)",
                    (equity, last_reb, start_eq, peak))
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
                "peak_equity": peak, "halt_reason": halt_reason, "flattened": flattened,
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


# ── review: reconstruct the equity curve and score it (read-only, offline) ──
#
# read_book() is a *snapshot*; this is the *review*. The equity curve over time
# isn't in the DB (mom_state only keeps the latest value) — it lives in the
# `[MOM] equity ...` log line written each cycle. So we parse the log for the
# curve and the DB for trades/turnover. Pure + offline; the BTC benchmark (which
# needs the network) is left to the caller. Used by scripts/momentum_review.py
# and the Telegram /review command.

_BLOCKS = "▁▂▃▄▅▆▇█"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_EQ_RE = re.compile(r"\[MOM\] equity (\d+(?:\.\d+)?)")


def _parse_curve(log_path: str) -> list[tuple[datetime, float, bool]]:
    """Pull (timestamp, equity, was_rebalance) out of the `[MOM] equity` lines."""
    pts: list[tuple[datetime, float, bool]] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if "[MOM] equity" not in ln:
                    continue
                mt, me = _TS_RE.match(ln), _EQ_RE.search(ln)
                if not (mt and me):
                    continue
                dt = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S")
                pts.append((dt, float(me.group(1)), "REBALANCED" in ln))
    except FileNotFoundError:
        pass
    return pts


def _max_drawdown(equities: list[float]) -> float:
    """Worst peak-to-trough drop along the curve, as a negative fraction."""
    peak, worst = float("-inf"), 0.0
    for e in equities:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1)
    return worst


def _cycle_vol(equities: list[float]) -> float:
    """Std-dev of cycle-to-cycle returns — how bumpy the ride is."""
    rets = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    return (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5


def sparkline(equities: list[float]) -> str:
    """Compact block sparkline of the curve (downsampled to ~80 cols)."""
    e = equities
    if len(e) > 80:
        step = len(e) / 80
        e = [equities[int(i * step)] for i in range(80)]
    lo, hi = min(e), max(e)
    if hi == lo:
        return _BLOCKS[0] * len(e)
    return "".join(_BLOCKS[int((v - lo) / (hi - lo) * (len(_BLOCKS) - 1))] for v in e)


def review_book(db_path: str = "data/momentum_paper.db",
                log_path: str = "logs/cero.log") -> dict:
    """Score the paper book: return + drawdown + turnover + curve metrics.

    Read-only and offline (no network). Returns {} if there's no book yet. The
    BTC buy-and-hold benchmark is intentionally NOT computed here — it needs the
    exchange; the caller adds it (the CLI builds its own client, the bot reuses
    the live one)."""
    bk = read_book(db_path)
    if not bk:
        return {}

    start = bk["start_equity"] or 0.0
    cur = bk["equity"]
    out: dict = {
        "start": start,
        "equity": cur,
        "total_ret": (cur / start - 1) if start else 0.0,
        "n_longs": len(bk["longs"]),
        "n_shorts": len(bk["shorts"]),
    }

    # trades → turnover + rebalance count (authoritative: distinct trade ts)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        trades = con.execute("SELECT ts, qty, price FROM mom_trades").fetchall()
    except sqlite3.OperationalError:
        trades = []
    finally:
        con.close()
    gross = sum(abs(q) * p for _, q, p in trades)
    out["n_fills"] = len(trades)
    out["turnover"] = gross
    out["turnover_x"] = (gross / start) if start else 0.0
    out["n_rebalances"] = len({ts for ts, _, _ in trades})

    # curve (from the logs) → span, peak/trough, drawdown, volatility, sparkline
    curve = _parse_curve(log_path)
    out["has_curve"] = bool(curve)
    if curve:
        eqs = [e for _, e, _ in curve]
        out["curve"] = eqs
        out["n_cycles"] = len(curve)
        out["first_dt"] = curve[0][0]
        out["last_dt"] = curve[-1][0]
        out["span_days"] = (curve[-1][0] - curve[0][0]).total_seconds() / 86400
        peak = max(curve, key=lambda p: p[1])
        trough = min(curve, key=lambda p: p[1])
        out["peak"] = (peak[1], peak[0])
        out["trough"] = (trough[1], trough[0])
        out["max_drawdown"] = _max_drawdown(eqs)
        out["cycle_vol"] = _cycle_vol(eqs)
        out["sparkline"] = sparkline(eqs)
    return out


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
