"""Per-criterion edge analysis — which of the 8 criteria actually predict wins?

The morning check shows tier A (the *highest* conviction tier) underperforming
tier B. That's an inversion: a higher score should mean a better trade. The only
way that happens is if one or more criteria are *anti-predictive* — passing them
correlates with LOSING — yet they carry enough weight to push a signal into A.

This script finds them empirically. For every tier-A/B signal of the chosen
strategy it resolves the outcome exactly like scripts/backtest_signals.py
(walk 5m candles, same-bar = loss, 24h horizon, realistic costs). Then, for each
of the 8 criteria, it splits the decided trades into "criterion passed" vs
"criterion failed" and reports the win rate + total R of each split.

  edge = WR(passed) - WR(failed)

  edge > 0  → criterion has predictive value (passing → more wins). Keep.
  edge ~ 0  → criterion is noise. Candidate to drop or re-weight.
  edge < 0  → criterion is ANTI-predictive (passing → fewer wins). These are
              the ones dragging tier A below tier B.

It also prints each criterion's pass-rate within tier A vs tier B, so you can see
*which* anti-predictive criteria are inflating scores into tier A.

Read-only. Uses raw sqlite3 so it can point at any DB file (e.g. one pulled off
the phone) without touching config.

Usage:
    uv run python scripts/criterion_edge.py --db data/cero_live.db
    uv run python scripts/criterion_edge.py --db data/cero_live.db --no-costs
    uv run python scripts/criterion_edge.py --db data/cero_live.db --strategy smc_trend
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict

# Match scripts/backtest_signals.py defaults exactly.
SLIPPAGE_PCT = 0.1   # per leg
FEE_PCT = 0.06       # per leg (bybit taker)


def resolve(direction, entry, sl, tp, candles) -> tuple[str, float]:
    """('win'|'loss'|'incomplete', r_multiple). candles = [(low, high), ...].
    SL checked before TP → same-bar ambiguity counts as a loss (conservative)."""
    if entry is None or sl is None or tp is None:
        return "incomplete", 0.0
    stop_dist = abs(entry - sl)
    tp_dist = abs(entry - tp)
    rr = tp_dist / stop_dist if stop_dist > 0 else 2.0
    for low, high in candles:
        if direction == "long":
            hit_sl, hit_tp = low <= sl, high >= tp
        else:
            hit_sl, hit_tp = high >= sl, low <= tp
        if hit_sl:
            return "loss", -1.0
        if hit_tp:
            return "win", rr
    return "incomplete", 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cero.db", help="sqlite DB path")
    ap.add_argument("--strategy", default="smc_trend")
    ap.add_argument("--tier", default="A,B")
    ap.add_argument("--horizon-hours", type=int, default=24)
    ap.add_argument("--no-costs", action="store_true")
    args = ap.parse_args()

    tiers = [t.strip() for t in args.tier.split(",")]
    horizon_ms = args.horizon_hours * 3600 * 1000

    con = sqlite3.connect(args.db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(signals)")]
    has_strat = "strategy" in cols

    placeholders = ",".join("?" * len(tiers))
    q = (
        "SELECT ts, symbol, tier, direction, entry_price, stop_loss, take_profit, "
        "criteria_json FROM signals "
        f"WHERE tier IN ({placeholders}) AND direction IN ('long','short') "
        "AND entry_price IS NOT NULL"
    )
    params: list = list(tiers)
    if has_strat and args.strategy:
        q += " AND strategy = ?"
        params.append(args.strategy)
    q += " ORDER BY ts ASC"
    sigs = con.execute(q, params).fetchall()

    # criterion -> {"P": [(result, r_real)], "F": [...]}
    by_crit: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(
        lambda: {"P": [], "F": []}
    )
    passrate: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    # overall tallies (reconcile against morning_check) + per-tier R
    wins = losses = incomplete = 0
    tier_outcomes: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for ts, symbol, tier, direction, entry, sl, tp, cj in sigs:
        cand = con.execute(
            "SELECT low, high FROM candles WHERE symbol=? AND timeframe='5m' "
            "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
            (symbol, ts, ts + horizon_ms),
        ).fetchall()
        result, r = resolve(direction, entry, sl, tp, cand)
        if result == "incomplete":
            incomplete += 1
            continue

        r_real = r
        if not args.no_costs:
            stop_dist = abs(entry - sl)
            if stop_dist > 0:
                cost_r = (entry * (SLIPPAGE_PCT + FEE_PCT) / 100 * 2) / stop_dist
                r_real = r - cost_r

        if result == "win":
            wins += 1
        else:
            losses += 1
        tier_outcomes[tier].append((result, r_real))

        try:
            crits = json.loads(cj) if cj else []
        except (TypeError, ValueError):
            crits = []
        for c in crits:
            name, passed = c.get("name"), bool(c.get("passed"))
            if not name:
                continue
            by_crit[name]["P" if passed else "F"].append((result, r_real))
            passrate[name][tier].append(passed)

    con.close()
    decided = wins + losses
    if decided == 0:
        print(f"no decided {args.strategy} trades in tiers {tiers} (db={args.db}).")
        return

    def wr(pairs: list[tuple[str, float]]) -> float:
        return (sum(1 for res, _ in pairs if res == "win") / len(pairs) * 100) if pairs else 0.0

    def totr(pairs: list[tuple[str, float]]) -> float:
        return sum(rv for _, rv in pairs)

    cost_note = "no-costs (ideal)" if args.no_costs else "realistic (slippage+fees)"
    print(f"=== Criterion edge — db={args.db}, strategy={args.strategy}, "
          f"tiers={tiers}, {cost_note} ===")
    print(f"decided: {decided}  ({wins}W / {losses}L)   incomplete: {incomplete}   "
          f"WR: {wins / decided * 100:.1f}%   total R: {totr(sum(tier_outcomes.values(), [])):+.2f}")
    print()
    print("the inversion, by tier:")
    for t in tiers:
        outs = tier_outcomes.get(t, [])
        if outs:
            print(f"  tier {t}: {len(outs):>3} trades, WR {wr(outs):5.1f}%, total R {totr(outs):+7.2f}")
    print()

    # ── edge table ──────────────────────────────────────────────────────
    rows = []
    for name, d in by_crit.items():
        p, f = d["P"], d["F"]
        has_both = bool(p) and bool(f)
        edge = (wr(p) - wr(f)) if has_both else None
        rows.append((name, len(p), wr(p), totr(p), len(f), wr(f), totr(f), edge))
    # sort: real edges first (most negative → worst), then n/a (gated) last
    rows.sort(key=lambda x: (x[7] is None, x[7] if x[7] is not None else 0))

    print(f"{'criterion':<18} {'n+':>4} {'WR+':>6} {'R+':>8}   "
          f"{'n-':>4} {'WR-':>6} {'R-':>8}   {'EDGE':>6}")
    print("-" * 80)
    for name, np_, wrp, rp, nf, wrf, rf, edge in rows:
        if edge is None:
            etxt, flag = "  n/a", "  (always passes — hard gate / near-constant)"
        else:
            etxt = f"{edge:>+6.1f}"
            flag = "  <== ANTI-PREDICTIVE" if edge < -4 else ("  <- ~noise" if abs(edge) <= 4 else "  edge")
        print(f"{name:<18} {np_:>4} {wrp:>5.1f}% {rp:>+8.2f}   "
              f"{nf:>4} {wrf:>5.1f}% {rf:>+8.2f}   {etxt}{flag}")

    # ── pass-rate by tier ───────────────────────────────────────────────
    print()
    print("pass-rate within each tier (what separates an A from a B):")
    print(f"{'criterion':<18} {'A pass%':>8} {'B pass%':>8}   {'A-B':>6}")
    print("-" * 48)
    gap_rows = []
    for name in by_crit:
        a = passrate[name].get("A", [])
        b = passrate[name].get("B", [])
        a_pct = (sum(a) / len(a) * 100) if a else 0.0
        b_pct = (sum(b) / len(b) * 100) if b else 0.0
        gap_rows.append((name, a_pct, b_pct, a_pct - b_pct))
    gap_rows.sort(key=lambda x: -abs(x[3]))
    for name, a_pct, b_pct, gap in gap_rows:
        print(f"{name:<18} {a_pct:>7.1f}% {b_pct:>7.1f}%   {gap:>+5.1f}")

    print()
    print("How to read it: a criterion with EDGE < 0 AND a large +A-B gap is")
    print("mechanically responsible for tier A < tier B — it fires mostly in A and")
    print("loses when it fires. Those are the candidates to drop, invert, or re-weight.")


if __name__ == "__main__":
    main()
