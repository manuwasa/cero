# Cero

A personal crypto trading assistant. Watches markets, scores setups against your rules, and (optionally) trades on your behalf — with a kill switch you can hit anytime.

> **Status:** working v1. Boots end-to-end against bybit testnet — backfills candles, scores setups with the 8 criteria, sends Telegram alerts, serves a live dashboard at `http://127.0.0.1:8765`. ~155 tests passing.

---

## What Cero is

Cero is a **rule-based trading bot** for crypto perpetual swaps. You define what a good setup looks like as a scoring checklist. Cero watches the market 24/7, scores every candidate setup, and acts based on your rules and a tier system (A = full size, B = half size, C/D = no trade).

Three modes, switchable by editing `config.yaml`:

| Mode | Behavior |
| --- | --- |
| **signal_only** | Cero alerts you. You place trades manually. |
| **approval** | Cero proposes trades. You tap ✅ or ❌ on Telegram. |
| **auto** | Cero places trades on its own within risk limits. |

You will start in **signal_only**. You will not move to **auto** until your strategy has passed your own validation gate (typical: 200+ trades, ≥55% win rate, positive PnL). See [`docs/VALIDATION.md`](docs/VALIDATION.md).

---

## What Cero is *not*

- Not a money printer. Strategies degrade. Markets change. You will lose money while learning.
- Not a black box. The 8-criteria scoring is rules **you** define and tweak.
- Not on-chain. Cero trades centralized exchanges via REST/WebSocket APIs.
- Not multi-user. One person, one account, one machine.

---

## Documentation

Three guides cover what you actually need:

- **[docs/SETUP.md](docs/SETUP.md)** — first-time setup. Bybit testnet, KYC, faucet, API key, Telegram bot, config. Plan ~30 minutes.
- **[docs/USAGE.md](docs/USAGE.md)** — daily operation. The three modes, dashboard tour, every Telegram command, when to TRIP, mainnet checklist.
- **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** — for whoever opens this codebase later. Project layout, testing patterns, how to add an exchange / criterion / mode / notifier, common gotchas.

The opinionated reading:

- **[docs/CRITERIA.md](docs/CRITERIA.md)** — the 8 scoring criteria explained.
- **[docs/VALIDATION.md](docs/VALIDATION.md)** — the 200-trade gate. Read **before** ever flipping `mode: auto`.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit, and why.

---

## Architecture at a glance

```
   DATA SOURCES          INGESTION         STATE        BRAIN          EXECUTION       OUTPUTS
   ──────────────        ─────────         ─────        ─────          ─────────       ───────
                  ┌─→  price_worker  ─┐
   Exchange       │                   │
   (via ccxt) ────┤    account_worker ┼──→ SQLite ──→  brain      ──→  executor   ──→  Telegram
                  │                   │                (8 criteria,    (signal/         dashboard
   RSS feeds ─────┼─→  news_worker   ─┤                tier scoring,    approval/       (FastAPI
                  │                   │                risk gates)      auto)            on :8765)
   ForexFactory ──┴─→  calendar_worker┘
```

**One Python process. One SQLite file. One folder.**

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full breakdown.

---

## Quick start

```bash
# 1. Install deps (uv recommended — see docs/SETUP.md if you don't have it)
uv sync

# 2. Copy and fill secrets
cp .env.example .env
# edit .env with bybit testnet API keys + Telegram bot token

# 3. Review strategy settings
# edit config.yaml — symbols, risk, criteria weights

# 4. Run
uv run python -m cero
```

You'll see:
- Telegram bot online (message it `/help` to see commands)
- Web dashboard at `http://127.0.0.1:8765`
- Logs streaming to `logs/cero.log`

**First-time setup gotchas** (each one bit us during build — full walkthrough in [docs/SETUP.md](docs/SETUP.md)):

- Bybit testnet derivatives needs **KYC** (Lv1 Lite Verification). Without it, orders fail with `retCode: 10024`.
- Testnet funds must be in the **Unified Trading Account**, not Spot or Funding — only Unified is queryable via the API.
- After creating your Telegram bot, you must **tap Start in the bot's chat** before it can DM you. Skipping this gives "chat not found".
- API key permissions: **Read + Trade only**. Never enable Withdraw.

---

## Project layout

```
cero/
├── .env                      # secrets (gitignored)
├── .env.example              # template for .env
├── config.yaml               # strategy & runtime settings
├── pyproject.toml            # Python project + deps
├── data/cero.db              # SQLite (created on first run)
│
├── cero/                     # the package
│   ├── main.py               # entry point, boots everything
│   ├── config.py             # config loader (pydantic)
│   ├── events.py             # in-process pubsub bus
│   │
│   ├── data/                 # ingestion workers
│   │   ├── exchange.py       # ccxt wrapper (the only file that imports ccxt)
│   │   ├── price_worker.py
│   │   ├── account_worker.py
│   │   ├── news_worker.py
│   │   └── calendar_worker.py
│   │
│   ├── db/
│   │   ├── models.py         # SQLAlchemy tables
│   │   └── session.py        # async engine + session factory
│   │
│   ├── brain/                # pure decision logic, no I/O
│   │   ├── indicators.py     # EMA, ATR, swings, BOS, OTE, FVG
│   │   ├── criteria.py       # the 8 checks + MarketContext
│   │   ├── scoring.py        # weight → tier
│   │   ├── direction.py      # long/short logic
│   │   ├── risk.py           # sizing, daily loss caps, TRIP
│   │   ├── signals.py        # Signal + build_signal()
│   │   └── scheduler.py      # the live evaluation loop
│   │
│   ├── exec/
│   │   ├── protocols.py      # Notifier + OrderPlacer abstractions
│   │   ├── modes.py          # signal_only / approval / auto + TripWatcher
│   │   ├── orders.py         # CcxtOrderPlacer
│   │   └── oco.py            # intentionally empty (bybit's brackets are OCO)
│   │
│   └── ui/
│       ├── telegram/
│       │   ├── bot.py        # TelegramNotifier + lifecycle
│       │   └── handlers.py   # slash commands
│       └── web/
│           ├── server.py     # FastAPI + WebSocket
│           └── static/       # vanilla HTML/JS/CSS dashboard
│
├── tests/                    # ~155 tests, pytest + pytest-asyncio
├── scripts/                  # smoke tests for each layer
├── docs/                     # see above
└── CLAUDE.md                 # read by Claude Code on open
```

---

## The 200-trade rule

Before you flip `mode: auto`:

- [ ] At least **200 trades** in `signal_only` or `approval` mode
- [ ] Win rate **≥ 55%** sustained
- [ ] **Positive cumulative PnL**
- [ ] Profit factor **≥ 1.5**
- [ ] Strategy is **stable** (no degradation between first 100 and last 100 trades)

Why 200, why 55%, why positive PnL — see [`docs/VALIDATION.md`](docs/VALIDATION.md). The math is real. Don't skip this.

---

## Safety rules (non-negotiable)

1. **Never** enable "Withdraw" on your exchange API key. Read + Trade only.
2. **Never** commit `.env`. It's in `.gitignore` — keep it that way.
3. **Start tiny.** Validate on $50–$200 of capital. Scale 10x at a time, with re-validation each step.
4. **`mode: signal_only` is the default.** Auto has to be explicitly enabled and earned.
5. **TRIP halts everything.** Use it whenever something feels off.

---

## License & disclaimer

This is a personal project. It is not financial advice. Trading derivatives can lose you all your money. You are responsible for your own decisions.
