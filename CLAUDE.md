# CLAUDE.md — instructions for Claude Code

This file is read by Claude Code when this project is opened. It tells Claude what Cero is, how it's built, and what the user needs help with next.

## Project: Cero

A personal crypto trading assistant. Single-user, retail-scale, runs as one Python process with an embedded FastAPI dashboard and a Telegram bot. Trades crypto perpetual swaps via ccxt (multi-exchange).

The user is building this to learn AND actually use it. Both matter. Optimize for:
- **Clarity** — code should teach, not just work
- **Correctness** — this touches real money
- **Safety** — every dangerous operation needs a gate
- **Iterability** — strategy rules will change constantly

## Current state

**Scaffold only.** The folder structure, `README.md`, `docs/`, `config.yaml`, `.env.example`, and `pyproject.toml` exist. The actual Python modules are mostly empty stubs.

The user has NOT yet:
- Created their exchange API keys
- Created their Telegram bot
- Picked their final exchange (they want multi-exchange via ccxt, but will pick a primary)
- Defined the exact thresholds for each criterion

## What to build first (in order)

1. **`cero/config.py`** — pydantic models that load `config.yaml` and `.env`, validate them, expose typed settings. Single source of truth.
2. **`cero/db/models.py`** — SQLAlchemy tables: `candles`, `accounts`, `positions`, `trades`, `news`, `calendar_events`, `signals`.
3. **`cero/data/exchange.py`** — a thin wrapper around ccxt async. Symbol normalization, candle fetching, account state, order placement. ALL exchange interaction goes through this module.
4. **`cero/data/price_worker.py`** — subscribes to WebSocket candles for each symbol in config, writes to DB.
5. **`cero/brain/criteria.py`** — implement the 8 criteria as pure functions. Each takes a `MarketContext` and returns `CriterionResult(passed: bool, score: int, detail: str)`.
6. **`cero/brain/scoring.py`** — aggregate criteria → tier (A/B/C/D) + direction.
7. **`cero/brain/risk.py`** — position sizing, daily loss cap, TRIP logic.
8. **`cero/exec/modes.py`** — the three modes (signal_only, approval, auto) as a strategy pattern.
9. **`cero/ui/telegram/bot.py`** — slash command handlers: `/pnl`, `/positions`, `/readiness`, `/economic`, `/trip`, `/reset`, `/pause`, `/resume`, `/set`.
10. **`cero/ui/web/server.py`** — FastAPI + WebSocket for live dashboard updates.
11. **`cero/main.py`** — boot everything as asyncio tasks.

## Key design principles

- **One process.** No microservices. asyncio tasks share state via the SQLite DB and in-memory state objects.
- **ccxt as the only exchange interface.** Never call exchange-specific SDKs directly. This keeps multi-exchange swappable.
- **Pure functions in the brain.** Criteria, scoring, risk math should be testable without touching the network.
- **Pydantic everywhere.** All boundaries (config, API responses, signals) are typed pydantic models.
- **Logs are structured.** Use loguru, include context (symbol, signal_id, mode) in every log line.
- **Errors don't kill the process.** Each worker should restart on failure with backoff. A dead Telegram bot must not stop the price worker.

## Files NOT to modify without asking

- `README.md` — user-facing project doc
- `docs/CRITERIA.md` — strategy spec; changes here change the math
- `docs/VALIDATION.md` — risk management philosophy

If a criterion needs to change, propose the change in chat first, then update `docs/CRITERIA.md` AND the code together.

## Testing

The user is learning, so favor:
- Small, runnable scripts that demonstrate a single piece working
- Pytest tests for pure functions (criteria, scoring, risk math)
- A "paper mode" that pipes synthetic candles through the brain without touching any exchange

Do NOT write tests that hit real exchange APIs unless explicitly asked.

## Conventions

- Python 3.11+
- `uv` for dependency management (not poetry, not pip-tools)
- `ruff` for linting + formatting
- Type hints on every function
- Async by default; sync only where it doesn't matter

## How to ask the user for input

When you hit a decision that needs their judgment (which exchange to default to, what the BTC stop distance should be, whether to start with 1H or 4H timeframe), **ask**. Don't guess. The user is here to learn — them making decisions IS the learning.

When you finish a chunk, summarize what was built and what's next. Short. No fluff.
