"""
Slash command handlers.

A handler reads state and writes a reply — never mutates trading state
without explicit confirmation. Destructive commands (`/trip`, `/reset`)
require typing the command exactly; we don't add a follow-up confirmation
keyboard because Telegram chats are already user-private.

Handlers depend on a `services` dict, populated by the caller in `bot.py`:
    {
      "risk_gate":  RiskGate instance,
      "config":     loaded Config (for /status, /readiness),
      "mode_provider": callable returning the current ExecutionMode (optional),
    }
Any missing service degrades the corresponding command to a polite "not
wired" reply rather than crashing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from sqlalchemy import desc, func, select

from cero.brain.momentum import read_book, review_book
from cero.db.models import Position, Signal as SignalRow, Trade, TripEvent
from cero.db.session import session_factory


def register(dp: Dispatcher, services: dict, allowed_chat_ids: set[str]) -> None:
    """Attach every command handler to `dp`. Caller injects `services`."""

    def authorized(msg: Message) -> bool:
        return str(msg.from_user.id) in allowed_chat_ids if msg.from_user else False

    log = logger.bind(component="telegram.handlers")
    cfg_obj = services.get("config")

    def momentum_mode() -> bool:
        return cfg_obj is not None and getattr(cfg_obj, "engine", "smc") == "momentum"

    def reb_days() -> int:
        return cfg_obj.momentum.rebalance_days if cfg_obj is not None else 5

    def universe_desc() -> str:
        if cfg_obj is None:
            return "momentum"
        m = cfg_obj.momentum
        return (f"auto top-{m.universe_size} liquid" if m.auto_universe
                else f"{len(m.universe)} fixed coins")

    # ── /start, /help ────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def _start(msg: Message) -> None:
        if not authorized(msg):
            log.warning("ignoring /start from unauthorized chat {}", msg.from_user.id)
            return
        await msg.reply(
            "👋 <b>Cero</b> is online.\n\n"
            "Type /help to see what I can do."
        )

    @dp.message(Command("help"))
    async def _help(msg: Message) -> None:
        if not authorized(msg):
            return
        await msg.reply(
            "<b>Commands</b>\n"
            "<code>/status     </code> health + equity snapshot\n"
            "<code>/review     </code> performance review (return, drawdown, vs BTC)\n"
            "<code>/book       </code> current long/short momentum holdings\n"
            "<code>/pnl        </code> equity + PnL\n"
            "<code>/positions  </code> current holdings (with sizes)\n"
            "<code>/readiness  </code> per-symbol view\n"
            "<code>/trip       </code> kill switch\n"
            "<code>/reset      </code> clear an active TRIP\n"
            "<code>/help       </code> this message"
        )

    # ── /status ──────────────────────────────────────────────────────

    @dp.message(Command("status"))
    async def _status(msg: Message) -> None:
        if not authorized(msg):
            return
        gate = services.get("risk_gate")
        if momentum_mode():
            bk = read_book()
            lines = ["<b>cero status</b> — engine <code>momentum</code> (paper)"]
            lines.append(f"  universe: {universe_desc()}, rebalance {reb_days()}d")
            if bk:
                pct = (bk["equity"] / bk["start_equity"] - 1) * 100 if bk["start_equity"] else 0.0
                lines.append(f"  equity <code>{bk['equity']:.2f}</code> ({pct:+.2f}%)  book {len(bk['longs'])}L/{len(bk['shorts'])}S")
                if bk["last_rebalance"]:
                    days = int((datetime.now(timezone.utc).timestamp() * 1000 - bk["last_rebalance"]) / 86_400_000)
                    lines.append(f"  last rebalance {days}d ago — next in ~{max(0, reb_days() - days)}d")
                longs = ", ".join(x.split("/")[0] for x in bk["longs"]) or "—"
                shorts = ", ".join(x.split("/")[0] for x in bk["shorts"]) or "—"
                lines.append(f"  <b>LONG</b>: {longs}")
                lines.append(f"  <b>SHORT</b>: {shorts}")
            else:
                lines.append("  book not started yet — engine hasn't rebalanced")
            if gate is not None and gate.tripped:
                lines.append(f"  ⛔ <b>TRIPPED</b>: {gate.trip_reason}")
            await msg.reply("\n".join(lines))
            return
        cfg = services.get("config")
        lines = ["<b>cero status</b>"]
        if cfg is not None:
            lines.append(
                f"  mode: <code>{cfg.mode}</code>  "
                f"exchange: <code>{cfg.exchange.name}</code>  "
                f"testnet: <code>{cfg.exchange.testnet}</code>"
            )
            lines.append(f"  symbols: <code>{', '.join(cfg.symbols)}</code>")
        if gate is not None:
            if gate.tripped:
                lines.append(f"  ⛔ <b>TRIPPED</b>: {gate.trip_reason} — {gate.trip_detail}")
            else:
                lines.append("  ✅ not tripped")
        await msg.reply("\n".join(lines))

    # ── /book (momentum holdings) ────────────────────────────────────

    @dp.message(Command("book"))
    async def _book(msg: Message) -> None:
        if not authorized(msg):
            return
        bk = read_book()
        if not bk:
            await msg.reply("no momentum book yet — the engine hasn't rebalanced.")
            return
        pct = (bk["equity"] / bk["start_equity"] - 1) * 100 if bk["start_equity"] else 0.0
        longs = ", ".join(x.split("/")[0] for x in bk["longs"]) or "—"
        shorts = ", ".join(x.split("/")[0] for x in bk["shorts"]) or "—"
        await msg.reply(
            f"<b>momentum book</b>  equity <code>{bk['equity']:.2f}</code> ({pct:+.2f}%)\n"
            f"<b>LONG</b> ({len(bk['longs'])}): {longs}\n"
            f"<b>SHORT</b> ({len(bk['shorts'])}): {shorts}"
        )

    # ── /review (performance review: return, drawdown, vs BTC) ────────

    @dp.message(Command("review"))
    async def _review(msg: Message) -> None:
        if not authorized(msg):
            return
        if not momentum_mode():
            await msg.reply("/review is for the momentum engine.")
            return
        log_path = cfg_obj.logging.file if cfg_obj is not None else "logs/cero.log"
        r = review_book(log_path=log_path)
        if not r:
            await msg.reply("no momentum book yet — the engine hasn't run a cycle.")
            return

        lines = ["<b>momentum review</b> (paper)"]
        if r["has_curve"]:
            lines.append(f"  window: {r['span_days']:.1f}d, {r['n_cycles']} cycles, {r['n_rebalances']} rebalances")
            lines.append(f"  equity <code>{r['equity']:.2f}</code> (start {r['start']:.0f}) → <b>{r['total_ret'] * 100:+.2f}%</b>")
            lines.append(f"  peak {r['peak'][0]:.0f} · trough {r['trough'][0]:.0f}")
            lines.append(f"  max drawdown <b>{r['max_drawdown'] * 100:+.1f}%</b> · cycle vol {r['cycle_vol'] * 100:.2f}%")
            lines.append(f"  turnover {r['turnover_x']:.1f}× over {r['n_fills']} fills")
            lines.append(f"  <code>{r['sparkline']}</code>")
        else:
            lines.append(f"  equity <code>{r['equity']:.2f}</code> → <b>{r['total_ret'] * 100:+.2f}%</b> (no curve in log yet)")

        # BTC benchmark via the already-connected exchange (best-effort)
        ex = services.get("exchange")
        span = r.get("span_days", 0.0)
        if ex is not None and span >= 0.5:
            days = max(1, round(span))
            try:
                candles = await ex.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=days + 3)
                closes = [c.close for c in candles]
                if len(closes) >= 2:
                    w = closes[-(days + 1):] if days + 1 <= len(closes) else closes
                    bench = w[-1] / w[0] - 1
                    gap = (r["total_ret"] - bench) * 100
                    verb = "beat" if gap >= 0 else "lagged"
                    lines.append(f"  vs hold BTC ~{days}d: {bench * 100:+.2f}% → <b>{verb}</b> by {abs(gap):.1f} pts")
            except Exception:  # noqa: BLE001 — benchmark is optional, never break /review
                pass

        lines.append(f"  book {r['n_longs']}L/{r['n_shorts']}S")
        if span < 14:
            lines.append(f"  <i>⚠ {span:.0f}d is noise — sanity check, not a verdict.</i>")
        await msg.reply("\n".join(lines))

    # ── /readiness ───────────────────────────────────────────────────

    @dp.message(Command("readiness"))
    async def _readiness(msg: Message) -> None:
        if not authorized(msg):
            return
        if momentum_mode():
            await msg.reply("momentum engine — no per-symbol tiers. Use /book for the current long/short holdings.")
            return
        cfg = services.get("config")
        symbols = cfg.symbols if cfg is not None else None

        async with session_factory()() as s:
            stmt = (
                select(SignalRow)
                .order_by(desc(SignalRow.ts))
                .limit(20)
            )
            rows = (await s.execute(stmt)).scalars().all()

        # Keep only the latest per symbol.
        latest_per_symbol: dict[str, SignalRow] = {}
        for r in rows:
            if r.symbol not in latest_per_symbol:
                latest_per_symbol[r.symbol] = r

        if not latest_per_symbol:
            await msg.reply("no signals yet — start the workers and wait a moment.")
            return

        ordered = symbols or sorted(latest_per_symbol.keys())
        lines = ["<b>latest readiness</b>"]
        for sym in ordered:
            r = latest_per_symbol.get(sym)
            if r is None:
                lines.append(f"  <code>{sym}: —</code>")
                continue
            age_s = int((datetime.now(timezone.utc).timestamp() * 1000 - r.ts) / 1000)
            lines.append(
                f"  <code>{sym}</code>  tier <b>{r.tier}</b>  "
                f"{r.direction}  score {r.score}  ({age_s}s ago)"
            )
        await msg.reply("\n".join(lines))

    # ── /positions ───────────────────────────────────────────────────

    @dp.message(Command("positions"))
    async def _positions(msg: Message) -> None:
        if not authorized(msg):
            return
        if momentum_mode():
            bk = read_book()
            if not bk or not bk["positions"]:
                await msg.reply("no momentum positions yet.")
                return
            lines = ["<b>momentum positions</b>"]
            for sym in sorted(bk["positions"]):
                sz, lp = bk["positions"][sym]
                lines.append(f"  <code>{sym.split('/')[0]}</code>  {'long' if sz > 0 else 'short'}  "
                             f"size {sz:+.4f}  @ {lp:.4f}")
            await msg.reply("\n".join(lines))
            return
        async with session_factory()() as s:
            rows = (await s.execute(select(Position).order_by(Position.symbol))).scalars().all()
        if not rows:
            await msg.reply("no open positions.")
            return
        lines = ["<b>open positions</b>"]
        for p in rows:
            lines.append(
                f"  <code>{p.symbol}</code>  {p.side}  size {p.size:+.4f}  "
                f"entry {p.entry_price:.2f}  uPnL {p.unrealized_pnl:+.2f}"
            )
        await msg.reply("\n".join(lines))

    # ── /pnl ─────────────────────────────────────────────────────────

    @dp.message(Command("pnl"))
    async def _pnl(msg: Message) -> None:
        if not authorized(msg):
            return
        if momentum_mode():
            bk = read_book()
            if not bk:
                await msg.reply("no momentum book yet.")
                return
            pct = (bk["equity"] / bk["start_equity"] - 1) * 100 if bk["start_equity"] else 0.0
            await msg.reply(
                "<b>PnL</b> (momentum, paper)\n"
                f"  equity: <code>{bk['equity']:.2f}</code>  "
                f"({pct:+.2f}% since {bk['start_equity']:.0f})\n"
                f"  rebalance trades logged: {bk['n_trades']}"
            )
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        day_start = now_ms - (now_ms % 86_400_000)

        async with session_factory()() as s:
            today_rows = (
                await s.execute(
                    select(Trade).where(Trade.closed_at >= day_start)
                )
            ).scalars().all()
            all_pnl = (
                await s.execute(select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)))
            ).scalar_one()
            total_count = (
                await s.execute(select(func.count()).select_from(Trade))
            ).scalar_one()

        today_pnl = sum(t.realized_pnl for t in today_rows)
        wins = sum(1 for t in today_rows if t.realized_pnl > 0)
        losses = sum(1 for t in today_rows if t.realized_pnl < 0)

        await msg.reply(
            "<b>PnL</b>\n"
            f"  today:   <code>{today_pnl:+.2f}</code>  "
            f"({wins}W / {losses}L of {len(today_rows)})\n"
            f"  all-time: <code>{float(all_pnl):+.2f}</code>  "
            f"({total_count} trades)"
        )

    # ── /trip ────────────────────────────────────────────────────────

    @dp.message(Command("trip"))
    async def _trip(msg: Message) -> None:
        if not authorized(msg):
            return
        gate = services.get("risk_gate")
        if gate is None:
            await msg.reply("risk gate not wired — can't trip.")
            return
        if gate.tripped:
            await msg.reply(
                f"already tripped: <code>{gate.trip_reason}</code> — {gate.trip_detail}"
            )
            return
        detail = (msg.text or "").removeprefix("/trip").strip() or "via telegram"
        await gate.trip("manual", detail)
        await msg.reply(f"⛔ <b>TRIPPED</b> — {detail}")

    # ── /reset ───────────────────────────────────────────────────────

    @dp.message(Command("reset"))
    async def _reset(msg: Message) -> None:
        if not authorized(msg):
            return
        gate = services.get("risk_gate")
        if gate is None:
            await msg.reply("risk gate not wired — can't reset.")
            return
        if not gate.tripped:
            await msg.reply("not tripped — nothing to reset.")
            return
        await gate.reset(by="user")
        await msg.reply("✅ TRIP cleared. Trading allowed again.")

    # ── /trips (history) ─────────────────────────────────────────────

    @dp.message(Command("trips"))
    async def _trips(msg: Message) -> None:
        if not authorized(msg):
            return
        async with session_factory()() as s:
            rows = (
                await s.execute(
                    select(TripEvent).order_by(desc(TripEvent.fired_at)).limit(10)
                )
            ).scalars().all()
        if not rows:
            await msg.reply("no trip history.")
            return
        lines = ["<b>recent trips</b>"]
        for t in rows:
            ts = datetime.fromtimestamp(t.fired_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            status = "active" if t.cleared_at is None else f"cleared by {t.cleared_by}"
            lines.append(f"  <code>{ts}</code>  {t.reason} — <i>{status}</i>")
        await msg.reply("\n".join(lines))
