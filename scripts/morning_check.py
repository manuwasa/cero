"""Daily 30-second status check during the validation observation period.

Prints a compact dashboard of where you stand on the 200-trade validation
gate. Designed to glance at while coffee brews.

Usage:
    uv run python scripts/morning_check.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import asc, func, select

from cero.config import load_config
from cero.db.models import (
    AccountSnapshot,
    Candle,
    Position,
    Signal,
    Trade,
)
from cero.db.session import close_db, init_db, session_factory


GATE_MIN_TRADES = 200
GATE_MIN_WR_PCT = 55.0
GATE_MIN_PF = 1.5
GATE_MAX_STABILITY_PP = 5.0


def _resolve(direction, entry, sl, tp, candles) -> tuple[str, float]:
    """Return ('win'|'loss'|'incomplete', r_multiple)."""
    if entry is None or sl is None or tp is None:
        return "incomplete", 0.0
    stop_dist = abs(entry - sl)
    tp_dist = abs(entry - tp)
    rr = tp_dist / stop_dist if stop_dist > 0 else 2.0
    for c in candles:
        if direction == "long":
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
        else:
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp
        if hit_sl and hit_tp:
            return "loss", -1.0
        if hit_sl:
            return "loss", -1.0
        if hit_tp:
            return "win", rr
    return "incomplete", 0.0


async def main() -> None:
    cfg, _ = load_config()
    await init_db(cfg.database)

    today = datetime.now(timezone.utc).date()

    async with session_factory()() as s:
        # Signals total + last 7 days
        n_sigs = (await s.execute(select(func.count()).select_from(Signal))).scalar_one()
        n_ab = (await s.execute(
            select(func.count()).select_from(Signal)
            .where(Signal.tier.in_(["A", "B"]))
        )).scalar_one()

        cutoff_7d = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
        n_ab_recent = (await s.execute(
            select(func.count()).select_from(Signal)
            .where(Signal.tier.in_(["A", "B"]))
            .where(Signal.ts >= cutoff_7d)
        )).scalar_one()

        # Open positions
        open_pos = (await s.execute(select(Position))).scalars().all()

        # Trades closed today (UTC)
        day_start = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000)
        today_trades = (await s.execute(
            select(Trade).where(Trade.closed_at >= day_start)
        )).scalars().all()

        # Most recent account snapshot
        last_snap = (await s.execute(
            select(AccountSnapshot).order_by(AccountSnapshot.ts.desc()).limit(1)
        )).scalar_one_or_none()

        # Backtest tier A/B signals
        sigs = (await s.execute(
            select(Signal)
            .where(Signal.tier.in_(["A", "B"]))
            .where(Signal.direction.in_(["long", "short"]))
            .order_by(asc(Signal.ts))
        )).scalars().all()

        wins = losses = incomplete = 0
        gross_w = gross_l = 0.0
        decided_sorted: list[str] = []   # 'win' or 'loss' in order
        for sig in sigs:
            if sig.entry_price is None:
                incomplete += 1
                continue
            end_ms = sig.ts + 24 * 3600 * 1000
            candles = (await s.execute(
                select(Candle)
                .where(Candle.symbol == sig.symbol)
                .where(Candle.timeframe == "5m")
                .where(Candle.open_time >= sig.ts)
                .where(Candle.open_time <= end_ms)
                .order_by(asc(Candle.open_time))
            )).scalars().all()
            result, r = _resolve(sig.direction, sig.entry_price, sig.stop_loss, sig.take_profit, candles)
            if result == "win":
                wins += 1
                gross_w += r
                decided_sorted.append("win")
            elif result == "loss":
                losses += 1
                gross_l += abs(r)
                decided_sorted.append("loss")
            else:
                incomplete += 1

    await close_db()

    decided = wins + losses
    wr = (wins / decided * 100) if decided else 0.0
    pf = (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
    total_r = gross_w - gross_l

    # Gate checks
    pass_count = decided >= GATE_MIN_TRADES
    pass_wr = wr >= GATE_MIN_WR_PCT
    pass_pf = pf >= GATE_MIN_PF
    pass_stable = False
    if decided >= 20:
        half = len(decided_sorted) // 2
        wr1 = sum(1 for r in decided_sorted[:half] if r == "win") / max(half, 1) * 100
        wr2 = sum(1 for r in decided_sorted[half:] if r == "win") / max(len(decided_sorted) - half, 1) * 100
        pass_stable = abs(wr1 - wr2) <= GATE_MAX_STABILITY_PP

    pct = lambda p: f"{p:>5.1f}%"
    check = lambda ok: "PASS" if ok else "fail"

    # ── output ──────────────────────────────────────────────────────────
    print(f"=== Cero — {today} ===")
    print(f"")
    print(f"signals: {n_sigs} total ({n_ab} tier A/B, {n_ab_recent} in last 7d)")
    if last_snap:
        print(f"equity:  {last_snap.equity:.2f} {last_snap.quote_currency}")
    print(f"open positions: {len(open_pos)}")
    if today_trades:
        today_pnl = sum(t.realized_pnl for t in today_trades)
        print(f"today trades:   {len(today_trades)}  realized {today_pnl:+.2f}")
    print()
    print(f"backtest (tier A/B, 24h horizon):")
    print(f"  decided:        {decided}W+L  ({wins}W / {losses}L, {incomplete} incomplete)")
    if decided > 0:
        print(f"  win rate:       {wr:.1f}%      total R: {total_r:+.2f}      PF: {pf:.2f}")
    print()
    print(f"validation gate progress:")
    print(f"  count >= 200:   {check(pass_count):<5} ({decided}/200)")
    print(f"  WR    >= 55%:   {check(pass_wr):<5} ({pct(wr)})")
    print(f"  PF    >= 1.5:   {check(pass_pf):<5} ({pf:.2f})")
    print(f"  stable:         {check(pass_stable):<5} {'(need 20+ trades to assess)' if decided < 20 else ''}")
    print()

    # Next milestone
    if decided < 20:
        print(f"next: collect to 20 trades to enable stability check (current: {decided})")
    elif decided < 50:
        print(f"next: collect to 50 trades before drawing strong conclusions (current: {decided})")
    elif decided < 200:
        if all([pass_wr, pass_pf, pass_stable]):
            print(f"next: keep going to 200 (current: {decided}) — stats are passing so far ✓")
        else:
            print(f"next: stats not passing at {decided} trades. Strategy may need revision.")
    else:
        if all([pass_count, pass_wr, pass_pf, pass_stable]):
            print("GATE PASSED — eligible to consider approval/auto modes.")
        else:
            print(f"GATE NOT PASSED — strategy doesn't have edge. Investigate or pivot.")


if __name__ == "__main__":
    asyncio.run(main())
