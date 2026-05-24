# Cero

A personal crypto trading assistant. Watches markets, scores setups against your rules, and (optionally) trades on your behalf — with a kill switch you can hit anytime.

> **Status:** scaffold / starter pack. Open this folder in Claude Code to actually build it out.

---

## What Cero is

Cero is a **rule-based trading bot** for crypto perpetual swaps. You define what a good setup looks like as a scoring checklist. Cero watches the market 24/7, scores every candidate setup, and acts based on your rules and a tier system (A = full size, B = half size, C/D = no trade).

Three modes, switchable at runtime:

| Mode | Behavior |
| --- | --- |
| **signal_only** | Cero alerts you. You place trades manually. |
| **approval** | Cero proposes trades. You tap ✅ or ❌ on Telegram. |
| **auto** | Cero places trades on its own within risk limits. |

You will start in **signal_only**. You will not move to **auto** until your strategy has passed your own validation gate (typical: 200+ trades, ≥55% win rate, positive PnL).

---

## What Cero is *not*

- Not a money printer. Strategies degrade. Markets change. You will lose money while learning.
- Not a black box. The 8-criteria scoring is rules **you** define and tweak.
- Not on-chain. Cero trades centralized exchanges via REST/WebSocket APIs.
- Not multi-user. One person, one account, one machine.

---

## Architecture at a glance

```
   DATA SOURCES          INGESTION         STATE        BRAIN          EXECUTION       OUTPUTS
   ──────────────        ─────────         ─────        ─────          ─────────       ───────
                  ┌─→  price_worker  ─┐
   Exchange       │                   │
   (via ccxt) ────┤    account_worker ┼──→ SQLite ──→  brain      ──→  executor   ──→  Telegram
                  │                   │                (8 criteria,    (signal/         dashboard
   Twitter   ─────┼─→  news_worker   ─┤                tier scoring,    approval/       (FastAPI
                  │                   │                risk gates)      auto)            on :8765)
   Calendar  ─────┴─→  calendar_worker┘
```

**One Python process. One SQLite file. One folder.**

See `docs/ARCHITECTURE.md` for the full breakdown.

---

## Quick start (once you've built it out in Claude Code)

```bash
# 1. Install deps (uv recommended)
uv sync

# 2. Copy and fill secrets
cp .env.example .env
# edit .env with your exchange API keys and Telegram token

# 3. Review strategy settings
# edit config.yaml — symbols, risk, criteria weights

# 4. Run
python -m cero
```

You'll see:
- Telegram bot online (message it `/pnl`, `/readiness BTC`, etc.)
- Web dashboard at `http://127.0.0.1:8765`
- Logs streaming to `logs/cero.log`

---

## Project layout

```
cero/
├── .env                      # secrets (gitignored)
├── .env.example              # template for .env
├── config.yaml               # strategy & runtime settings
├── pyproject.toml            # Python project + deps
├── cero.db                   # SQLite (created on first run)
│
├── cero/                     # the package
│   ├── main.py               # entry point, boots everything
│   ├── config.py             # config loader (pydantic)
│   │
│   ├── data/                 # ingestion workers
│   │   ├── exchange.py       # ccxt wrapper (works with OKX/Bybit/Binance)
│   │   ├── price_worker.py
│   │   ├── account_worker.py
│   │   ├── news_worker.py
│   │   └── calendar_worker.py
│   │
│   ├── db/
│   │   ├── models.py         # SQLAlchemy tables
│   │   └── queries.py
│   │
│   ├── brain/
│   │   ├── criteria.py       # the 8 checks
│   │   ├── scoring.py        # weight → tier
│   │   ├── direction.py      # long/short logic
│   │   ├── risk.py           # sizing, daily loss caps, TRIP
│   │   └── signals.py        # when to fire
│   │
│   ├── exec/
│   │   ├── modes.py          # signal_only / approval / auto
│   │   ├── orders.py         # place/cancel via ccxt
│   │   └── oco.py            # SL/TP attachment
│   │
│   └── ui/
│       ├── telegram/
│       │   ├── bot.py
│       │   └── handlers.py
│       └── web/
│           ├── server.py     # FastAPI app
│           └── static/       # HTML/JS/CSS dashboard
│
├── docs/
│   ├── ARCHITECTURE.md       # how the pieces fit
│   ├── CRITERIA.md           # the 8 scoring criteria, explained
│   └── VALIDATION.md         # the 200-trade gate, sample size math
│
└── CLAUDE.md                 # read by Claude Code on open
```

---

## The 200-trade rule

Before you flip `auto_trade` on:

- [ ] At least **200 trades** in `signal_only` or `approval` mode
- [ ] Win rate **≥ 55%** sustained
- [ ] **Positive cumulative PnL**
- [ ] Strategy is **stable** (no degradation between first 100 and last 100 trades)

Why 200, why 55%, why positive PnL — see `docs/VALIDATION.md`. The math is real. Don't skip this.

---

## Safety rules (non-negotiable)

1. **Never** enable "Withdraw" on your exchange API key. Read + Trade only.
2. **Never** commit `.env`. It's in `.gitignore` — keep it that way.
3. **Start tiny.** Validate on $50–$200 of capital. Scale 10x at a time, with re-validation each step.
4. **Auto mode is off by default.** Has to be explicitly enabled in `config.yaml`.
5. **TRIP halts everything.** Use it whenever something feels off.

---

## License & disclaimer

This is a personal project. It is not financial advice. Trading derivatives can lose you all your money. You are responsible for your own decisions.
