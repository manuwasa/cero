"""Momentum morning check — read-only status of the long/short paper book.

The momentum equivalent of scripts/morning_check.py (which is for the old smc
engine and won't reflect momentum). This ONLY reads data/momentum_paper.db —
no trades, no writes — so it's safe to run anytime, even while the engine is
live. Glance at it with your coffee.

Usage:
    uv run python scripts/momentum_check.py
    .venv/bin/python scripts/momentum_check.py        # on the phone
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from cero.config import load_config

DB = "data/momentum_paper.db"
DAY_MS = 86_400_000


def main() -> None:
    cfg, _ = load_config()
    reb = cfg.momentum.rebalance_days
    n_uni = len(cfg.momentum.universe)

    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)   # read-only
    except sqlite3.OperationalError:
        print(f"no momentum book yet at {DB} — has the engine run?")
        print("start it with `engine: momentum` in config.yaml, then `python -m cero`.")
        return

    row = con.execute("SELECT equity, last_rebalance, start_equity FROM mom_state WHERE id=1").fetchone()
    if not row:
        print("no momentum book yet — the engine hasn't rebalanced once. Give it a minute after start.")
        con.close()
        return
    equity, last_reb, start = row
    positions = con.execute("SELECT symbol, size FROM mom_positions").fetchall()
    n_trades = con.execute("SELECT COUNT(*) FROM mom_trades").fetchone()[0]
    con.close()

    longs = sorted(s for s, sz in positions if sz > 0)
    shorts = sorted(s for s, sz in positions if sz < 0)
    now = datetime.now(timezone.utc)
    pct = (equity / start - 1) * 100 if start else 0.0
    days_since = int((now.timestamp() * 1000 - last_reb) / DAY_MS) if last_reb else None

    print(f"=== Cero momentum — {now:%Y-%m-%d %H:%M} UTC ===\n")
    print(f"engine: momentum  (long/short, {n_uni}-coin universe, rebalance every {reb}d)")
    print(f"paper equity: {equity:.2f}   ({pct:+.2f}% since start of {start:.0f})")
    print(f"book: {len(longs)} long / {len(shorts)} short    trades logged: {n_trades}")
    if days_since is not None:
        when = datetime.fromtimestamp(last_reb / 1000, tz=timezone.utc)
        print(f"last rebalance: {when:%Y-%m-%d %H:%M} ({days_since}d ago) — next in ~{max(0, reb - days_since)}d")
    print()
    print("LONG : " + (", ".join(x.split('/')[0] for x in longs) or "—"))
    print("SHORT: " + (", ".join(x.split('/')[0] for x in shorts) or "—"))
    print("\nnote: equity is as of the last engine cycle (updates every ~6h). Paper — no real money.")


if __name__ == "__main__":
    main()
