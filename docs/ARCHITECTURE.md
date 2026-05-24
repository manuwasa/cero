# Architecture

How Cero is put together. Read this before changing structural things.

## The big picture

Cero is a **modular monolith**. One Python process, multiple internal modules, shared state via SQLite and in-memory objects.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Cero (one Python process)                     │
│                                                                      │
│   ┌─────────────────┐                                                │
│   │ DATA WORKERS    │   ── asyncio tasks ──                          │
│   │                 │                                                │
│   │ price_worker    │ ──► WebSocket candles from exchange (ccxt)     │
│   │ account_worker  │ ──► REST polling of balance/positions          │
│   │ news_worker     │ ──► scrape Twitter / RSS                       │
│   │ calendar_worker │ ──► scrape ForexFactory / FRED                 │
│   │                 │                                                │
│   └────────┬────────┘                                                │
│            │ writes                                                  │
│            ▼                                                         │
│   ┌─────────────────┐                                                │
│   │ SQLite DB       │  ◄── single source of truth for state          │
│   │ + in-mem cache  │                                                │
│   └────────┬────────┘                                                │
│            │ reads                                                   │
│            ▼                                                         │
│   ┌─────────────────┐                                                │
│   │ BRAIN           │                                                │
│   │                 │                                                │
│   │ criteria.py     │  pure functions, no I/O                        │
│   │ scoring.py      │  → tier (A/B/C/D), direction (long/short/none) │
│   │ risk.py         │  → sizing, daily loss cap, TRIP                │
│   │ signals.py      │  → emits Signal events on rule change          │
│   │                 │                                                │
│   └────────┬────────┘                                                │
│            │ signals                                                 │
│            ▼                                                         │
│   ┌─────────────────┐                                                │
│   │ EXECUTOR        │                                                │
│   │                 │                                                │
│   │ mode dispatch:  │                                                │
│   │  • signal_only  │ → just notify                                  │
│   │  • approval     │ → ask user, wait                               │
│   │  • auto         │ → place order via ccxt                         │
│   │                 │                                                │
│   └────────┬────────┘                                                │
│            │ orders + state changes                                  │
│            ▼                                                         │
│   ┌─────────────────┐                                                │
│   │ OUTPUTS         │                                                │
│   │                 │                                                │
│   │ Telegram bot    │ ─── push alerts, accept commands               │
│   │ FastAPI web     │ ─── live dashboard on :8765                    │
│   │                 │                                                │
│   └─────────────────┘                                                │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Why one process

For a single-user trading bot:

- **Latency** matters more than scale. Function calls beat HTTP between services.
- **Debugging** is dramatically easier. One log, one stack trace, one place to attach a debugger.
- **State sharing** is trivial. Workers update DB rows; brain reads them. No message broker needed.
- **Deployment** is `python -m cero`. No docker-compose, no service mesh.

The downside — one bug can crash everything. We mitigate by:
- Each worker is its own asyncio task with try/except + exponential backoff restart
- The brain runs scheduled (not on every tick) so it can't be flooded
- Telegram bot failures don't propagate up (notification is best-effort)

## Module boundaries

Each top-level module has one responsibility and a defined interface.

| Module | Reads | Writes | Calls |
| --- | --- | --- | --- |
| `data/exchange.py` | config | nothing | ccxt → external |
| `data/price_worker.py` | exchange | `candles` table | exchange |
| `data/account_worker.py` | exchange | `accounts`, `positions` | exchange |
| `data/news_worker.py` | external | `news` | external |
| `data/calendar_worker.py` | external | `calendar_events` | external |
| `brain/criteria.py` | `candles` | nothing | nothing (pure) |
| `brain/scoring.py` | criteria results | nothing | nothing (pure) |
| `brain/risk.py` | `accounts`, `trades`, config | nothing | nothing (pure) |
| `brain/signals.py` | all of brain | `signals` | nothing |
| `exec/modes.py` | `signals`, config | nothing | telegram, executor |
| `exec/orders.py` | `signals`, `accounts` | `trades`, `positions` | exchange |
| `ui/telegram/bot.py` | all tables | `signals` (approvals) | brain queries |
| `ui/web/server.py` | all tables | nothing | brain queries |

Two rules:
1. **Brain never does I/O.** It reads from passed-in dataclasses and returns dataclasses. This makes it testable.
2. **Everything exchange-specific lives in `data/exchange.py`.** If you want to add Bybit, you only touch one file.

## Lifecycle of a signal

This is the most important flow to understand. Trace it:

```
1. price_worker receives new 1H candle for BTC from exchange WebSocket
   → writes to `candles` table
   → publishes "candle:BTC:1h" event on internal pubsub

2. brain's scheduler subscribes to candle events; triggers evaluation
   → loads recent candles from DB
   → loads account state, recent trades, news
   → builds MarketContext (a frozen dataclass)

3. criteria.evaluate_all(ctx) runs the 8 checks
   → returns list[CriterionResult]
   → each has (passed, weight, detail)

4. scoring.aggregate(results) computes total score
   → maps to tier (A/B/C/D)
   → determines direction (long/short/none)

5. risk.size(account, tier, config) returns position size
   → applies tier modifier (A=1x, B=0.5x, C=0x)
   → applies daily loss cap
   → applies news blackout
   → returns 0 if any gate fails

6. signals.emit_if_changed(symbol, tier, direction, size)
   → if state changed meaningfully, writes Signal to DB
   → publishes "signal:new" event

7. exec.modes dispatches based on configured mode:
   → signal_only: telegram.send("READINESS BTC tier B short 0.5x")
   → approval:    telegram.ask("Approve trade?") and wait for callback
   → auto:        orders.place(signal) immediately

8. If trade placed:
   → orders.place(signal) calls exchange.create_order via ccxt
   → also places OCO algo orders for SL/TP
   → on fill (via WebSocket), updates `positions` and `trades`
   → telegram.send("Position opened")
   → dashboard pushes update via WebSocket
```

If anything fails at any step, the system stays in a safe state: no half-placed orders, no orphaned stops, no silent failures.

## State management

Three layers:

1. **SQLite (`cero.db`)** — persistent, durable. Survives restarts. Single source of truth for: candles, account balance history, positions, trades, signals, news, calendar events.

2. **In-memory state (`cero/state.py`)** — derived, fast. The current tier per symbol, the live account snapshot, the trip status, the active mode. Rebuilt from DB on startup.

3. **PubSub events (`cero/events.py`)** — transient. Used to decouple "candle arrived" from "brain evaluates" from "executor acts." Implemented as an asyncio.Queue or asyncio-pubsub library.

## The TRIP system

TRIP is the kill switch. When tripped:
- `state.tripped = True` is set
- All open orders are cancelled
- All open positions are closed at market
- No new signals will be acted on
- A red banner appears in the dashboard
- A Telegram alert is sent

Triggers (any one trips):
- Manual: `/trip` command or dashboard button
- Daily loss exceeds `config.risk.max_daily_loss_pct`
- Consecutive losses exceed `config.risk.max_consecutive_losses`
- Exchange API has returned errors above threshold in last N minutes
- Unexpected position appears (someone trading the same account)

Only un-trips via explicit `/reset` command. Never auto-resets.

## Three modes

Implemented via a strategy pattern in `exec/modes.py`:

```python
class ExecutionMode(Protocol):
    async def handle_signal(self, signal: Signal) -> None: ...

class SignalOnlyMode:
    async def handle_signal(self, signal):
        await telegram.send_signal_alert(signal)

class ApprovalMode:
    async def handle_signal(self, signal):
        approved = await telegram.request_approval(signal, timeout=60)
        if approved:
            await orders.place(signal)

class AutoMode:
    async def handle_signal(self, signal):
        if signal.tier in ("A", "B") and not state.tripped:
            await orders.place(signal)
```

Mode is set in `config.yaml` and can be changed live via `/mode signal_only|approval|auto`.

## Why ccxt instead of OKX-direct

The original Elysia used OKX directly. Cero uses ccxt for these reasons:

- **Portability.** Swap to Bybit or Binance in one config line.
- **Uniform API.** Same code for spot, perp, futures across exchanges.
- **Maintained.** ccxt has tons of contributors; exchange API changes get patched fast.
- **No re-learning.** Once you know ccxt, you know 100+ exchanges.

Tradeoff: ccxt is the lowest-common-denominator API. Exchange-specific features (OKX's algo orders, Binance's batch orders) need fallbacks. For Cero's needs (basic perp trading), ccxt covers 100% of what we need.

## What this architecture optimizes for

- ✅ Fast iteration on strategy rules (the brain is pure, isolated)
- ✅ Cheap to run (one process, $5/mo VPS)
- ✅ Easy to debug (one log, one stack trace)
- ✅ Safe to operate (TRIP, gates, modes)
- ✅ Extensible (new exchange = one file)

## What this architecture explicitly does NOT optimize for

- ❌ Microsecond latency (we're not HFT)
- ❌ Multiple users (single-user only)
- ❌ Massive scale (1 user, ~10 symbols max)
- ❌ Hot deployment (restart is fine, it's daily not hourly)
- ❌ Multi-machine deployment (one process, one box)

If any of those ever become real requirements, the architecture changes. Until then, simple wins.
