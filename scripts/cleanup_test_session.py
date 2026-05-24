"""Delete Signal + Trade rows from the auto-mode test session.

The auto-test session today exercised a pathological loop: rapid SOL shorts
that immediately SL'd. Those Trade rows shouldn't count toward the 200-trade
validation gate.

Usage:
    uv run python scripts/cleanup_test_session.py              # dry run (default)
    uv run python scripts/cleanup_test_session.py --commit     # actually delete

Filters (all are AND'd together — at least one must be set in --commit mode):
    --since "2026-05-24 19:00"   # cutoff timestamp; rows newer than this are candidates
    --symbol SOL/USDT:USDT        # only this symbol
    --tier B,C                    # only these tiers (comma-separated)
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete, select

from cero.config import load_config
from cero.db.models import Signal, Trade
from cero.db.session import close_db, init_db, session_factory


def _parse_dt(s: str) -> int:
    """Accept 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS' (assumed local time)."""
    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
    dt = datetime.strptime(s, fmt).astimezone()
    return int(dt.timestamp() * 1000)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since", default="2026-05-24 19:00",
        help="cutoff in local time, e.g. '2026-05-24 19:00'",
    )
    parser.add_argument("--symbol", default=None, help="filter to one symbol")
    parser.add_argument(
        "--tier", default=None,
        help="comma-separated tiers to match (e.g. 'B,C')",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="actually delete; default is dry-run preview only",
    )
    args = parser.parse_args()

    since_ms = _parse_dt(args.since)
    tiers = [t.strip() for t in args.tier.split(",")] if args.tier else None

    cfg, _ = load_config()
    await init_db(cfg.database)

    async with session_factory()() as s:
        # Find candidate signals
        sig_q = select(Signal).where(Signal.ts >= since_ms)
        if args.symbol:
            sig_q = sig_q.where(Signal.symbol == args.symbol)
        if tiers:
            sig_q = sig_q.where(Signal.tier.in_(tiers))
        signals = (await s.execute(sig_q)).scalars().all()

        # Find candidate trades (use opened_at as the time anchor — closed_at
        # could be much later for trades that took hours to close).
        trade_q = select(Trade).where(Trade.opened_at >= since_ms)
        if args.symbol:
            trade_q = trade_q.where(Trade.symbol == args.symbol)
        trades = (await s.execute(trade_q)).scalars().all()

        print(f"=== Cleanup preview ===")
        print(f"  since:  {args.since}  (>= {since_ms} ms)")
        print(f"  symbol: {args.symbol or 'ALL'}")
        print(f"  tiers:  {','.join(tiers) if tiers else 'ALL'}")
        print(f"")
        print(f"signals matched: {len(signals)}")
        per_tier: dict[str, int] = {}
        for sig in signals:
            per_tier[sig.tier] = per_tier.get(sig.tier, 0) + 1
        for t, n in sorted(per_tier.items()):
            print(f"  tier {t}: {n}")

        print(f"")
        print(f"trades matched: {len(trades)}")
        if trades:
            pnl = sum(t.realized_pnl for t in trades)
            wins = sum(1 for t in trades if t.realized_pnl > 0)
            losses = sum(1 for t in trades if t.realized_pnl < 0)
            print(f"  total realized_pnl: {pnl:+.2f}")
            print(f"  win/loss: {wins}/{losses}")

        if not args.commit:
            print()
            print("DRY RUN — nothing deleted. Pass --commit to actually delete.")
            await close_db()
            return

        # Commit phase — delete in the right order (trades first, signals
        # second, since trade.signal_id may FK to signal.id).
        if not signals and not trades:
            print("\nnothing to delete.")
            await close_db()
            return

        # Confirmation prompt
        print()
        confirm = input(f"Delete {len(signals)} signals + {len(trades)} trades? [yes/NO]: ")
        if confirm.strip().lower() != "yes":
            print("aborted.")
            await close_db()
            return

        sig_ids = [sig.id for sig in signals]
        trade_ids = [t.id for t in trades]
        if trade_ids:
            await s.execute(delete(Trade).where(Trade.id.in_(trade_ids)))
        if sig_ids:
            await s.execute(delete(Signal).where(Signal.id.in_(sig_ids)))
        await s.commit()
        print(f"deleted {len(trades)} trades + {len(signals)} signals.")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
