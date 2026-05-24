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
| `brain/scheduler.py` | all of `data/` tables | `signals` | brain pure-fns |
| `exec/modes.py` | `signals`, config | `signals.executed` | notifier, placer |
| `exec/orders.py` | live exchange state | `positions` | exchange |
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

5. risk.RiskGate.size_for(...) returns a SizingDecision
   → applies tier modifier (A=1x, B=0.5x, C=0x)
   → applies daily loss cap
   → applies news blackout (from calendar_events)
   → applies max concurrent positions
   → returns size=0 + a reason string if any gate fails

6. signals.build_signal(ctx, report, risk_gate, ...) → Signal
   → fills entry / stop_loss / take_profit using ATR(H1), clamped to 0.3–3% of price
   → persists to the `signals` table via persist_signal()
   → scheduler publishes "signal:new" event for actionable tiers

7. exec.modes dispatches based on configured mode:
   → signal_only: notifier.send_signal(signal)
   → approval:    notifier.request_approval(signal, timeout_s); place if ✅
   → auto:        if A/B and not tripped → placer.place(signal)

8. If trade placed:
   → CcxtOrderPlacer.place() calls exchange.create_market_order via ccxt
   → SL/TP attached as native position-level brackets in `params`
     (bybit/most modern perps handle these as OCO at the position level)
   → writes a Position row + flips signals.executed = True
   → account_worker reconciles open positions every 10s
   → telegram + dashboard refresh on next poll
```

If anything fails at any step, the system stays in a safe state: no half-placed orders, no orphaned stops, no silent failures.

## State management

Three layers:

1. **SQLite (`cero.db`)** — persistent, durable. Survives restarts. Single source of truth for: candles, account balance history, positions, trades, signals, news, calendar events.

2. **In-memory state** — derived, fast. Not a separate module; held as
   attributes on the long-lived objects in `cero/main.py`:
   - `RiskGate` (in `cero/brain/risk.py`) holds the trip state — and
     hydrates from the `trips` table on startup so a restart doesn't
     accidentally un-trip.
   - `PriceWorker`, `AccountWorker`, etc. hold their own task handles.
   - The active `ExecutionMode` is built once at boot from `cfg.mode`.

3. **PubSub events (`cero/events.py`)** — transient. Used to decouple "candle arrived" from "brain evaluates" from "executor acts." Implemented as `asyncio.Queue` per subscriber, **best-effort delivery** (full queues drop with a warning). Workers always write to the DB first and publish second, so a missed event never means lost state.

## The TRIP system

TRIP is the kill switch, implemented as `RiskGate` in `cero/brain/risk.py`.
When tripped:
- `RiskGate.tripped` flips to True; a `TripEvent` row is inserted.
- `RiskGate.trip()` publishes `trip:fired` on the bus.
- `TripWatcher` (in `cero/exec/modes.py`) reacts: cancels every open order
  and closes every open position via the `OrderPlacer`.
- `RiskGate.size_for(...)` returns 0 with `blocked_by="tripped"` for every
  signal, so no new trades enter even if the brain emits them.
- The dashboard shows a red banner; Telegram sends a notice.

Triggers (any one trips):
- **Manual**: `/trip` Telegram command or dashboard button.
- **Daily loss** exceeds `config.risk.max_daily_loss_pct` (default 3%).
- **Consecutive losses** reach `config.risk.max_consecutive_losses` (default 4).
- **Unexpected position** appears on the exchange — detected by
  `account_worker` reconciliation. The first poll imports any existing
  positions silently; only positions appearing *after* boot trip.

Only un-trips via explicit `/reset` command (Telegram or dashboard). Never
auto-resets. `RiskGate.hydrate()` reads the most recent un-cleared trip
row on boot, so a restart preserves trip state.

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

Mode is set at boot from `config.yaml`. The `Notifier` and `OrderPlacer`
are injected as Protocols (see `cero/exec/protocols.py`) — concrete
implementations are `TelegramNotifier` + `CcxtOrderPlacer` in production,
or `LogNotifier` + `StubOrderPlacer` for tests and signal-only runs.

Runtime mode hot-swap (`/mode signal_only|approval|auto`) is an open
enhancement — currently you stop, edit `config.yaml`, and restart.

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
