# Usage — running & operating Cero

Cero runs a **daily long/short momentum** strategy: it ranks a broad,
auto-discovered universe of crypto perps by recent momentum, goes **long the
strongest / short the weakest**, and rebalances every ~5 days. It's **paper
only** right now — no real money. One process, started with `start.sh` /
`python -m cero`.

First-time setup (exchange ids, optional Telegram bot, `.env`): see
[`docs/SETUP.md`](SETUP.md).

---

## ⚠️ Reality check (read once)

- Momentum is the one edge that survived honest validation (it beat buy-and-hold
  across ~2 years, 40+ coins, and two exchanges). But it is **modest and
  parameter-sensitive** — expect live results **weaker than the backtest**
  (survivorship bias; it's volatile, with ~30–60% drawdowns).
- It's **paper.** Real money comes only after weeks of forward paper results that
  actually hold up. Early ups/downs are noise — judge it over weeks, not days.

---

## What you need

- **For paper: nothing.** Chart data is public — no API keys required.
- **Telegram (optional):** if `.env` has a bot token you get rebalance alerts +
  commands; otherwise Cero just logs to the console/file.
- **Reachable data source:** the data exchange (`data_exchange: bybit`) must be
  reachable from wherever Cero runs (see [Reachability](#reachability)).

---

## Running it

**On a computer:**
```bash
python -m cero            # if the venv is active
# or:
uv run python -m cero
```
Stop with **Ctrl+C** (clean shutdown).

**On the phone (Termux), 24/7 with auto-restart** — use the watchdog under tmux:
```bash
tmux new -s cero
bash start.sh             # launches Cero, auto-restarts it if it ever exits
#   detach: Ctrl+b then d   ·   reattach: tmux attach -t cero
bash stop.sh              # clean stop, no restart
```

### A healthy boot looks like
```
connected: NN data markets — data from bybit, orders via binance
momentum engine started — universe: auto (top-50 liquid), rebalance 5d, equity 10000
Cero up — engine=momentum (daily long/short paper)
auto-universe: NN liquid perps
[MOM] equity 10000 (+0.0%) day +0 | 13L/13S  REBALANCED
```
Seeing `connected … data from bybit` and `[MOM] … REBALANCED` means it's working.

### Reachability
The **data** source must be reachable; the **trading** venue need not be (paper
places no orders). `config.yaml` ships with `data_exchange: bybit`,
`name: binance`. If your current network blocks Bybit, boot fails on the data
load — run it where Bybit is reachable (e.g. home wifi / VPN), or change
`data_exchange` (e.g. `okx`, though okx mixes tokenized stocks into the
universe). The watchdog auto-restarts, so brief outages self-heal.

**Only one instance per Telegram bot token.** Run on the phone *or* the PC, not
both at once — two pollers on one bot token collide (`TelegramConflictError`).

---

## Deploying an update (laptop → phone)

You edit and commit on the laptop; the phone pulls and restarts:
```bash
ssh u0_a585@192.168.0.3 -p 8022     # your phone's Termux SSH: user@ip -p port
cd ~/cero                            #   (the ip changes when the phone changes wifi)
bash stop.sh
git pull
bash start.sh
```
**Do not wipe the DB on a normal restart.** The momentum book lives in
`data/momentum_paper.db` and *should* persist across restarts — that's how it
accumulates a track record. Only clear it for a deliberate fresh start.

### Watchdog scripts (device-local — this is the canonical copy)
These live on the device (not in git). Recreate them from here if needed.

`start.sh`:
```bash
#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python"
mkdir -p logs; rm -f .cero_stop; echo $$ > .cero_watchdog.pid
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
trap 'rm -f .cero_watchdog.pid; command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock' EXIT
while [ ! -f .cero_stop ]; do
  echo "$(date '+%F %T') starting cero" | tee -a logs/watchdog.log
  "$PY" -m cero
  [ -f .cero_stop ] && break
  echo "$(date '+%F %T') exited — restart in 5s" | tee -a logs/watchdog.log
  sleep 5
done
rm -f .cero_stop
```
`stop.sh`:
```bash
#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
touch .cero_stop
pkill -f "python -m cero" 2>/dev/null
[ -f .cero_watchdog.pid ] && kill "$(cat .cero_watchdog.pid)" 2>/dev/null
echo "stop signal sent."
```

---

## Checking how it's doing

- **Terminal (read-only, safe anytime):**
  ```bash
  .venv/bin/python scripts/momentum_check.py
  ```
  Shows paper equity, % since start, the current long/short book, and days to the
  next rebalance.
- **Telegram:** `/status` (equity + book summary), `/book` (full long/short list),
  `/pnl`, `/help`. Plus an automatic `[MOM] … REBALANCED` push every ~5 days.
- **Logs:** `grep MOM ~/cero/logs/cero.log | tail` — the latest `[MOM]` line
  (printed each ~6h cycle).

---

## Config knobs ([`config.yaml`](../config.yaml))

| Setting | What it does |
| --- | --- |
| `exchange.name` | trading venue (binance) — for real orders, later |
| `exchange.data_exchange` | where **chart data** comes from (bybit) — separate from trading |
| `engine` | `momentum` (active) or `smc` (old per-symbol strategy, no edge — fallback only) |
| `momentum.auto_universe` | `true` = auto-pick the most-liquid coins (no fixed list) |
| `momentum.universe_size` / `min_volume_usd` | how many coins, and the liquidity floor |
| `momentum.rebalance_days` / `lookbacks` / `paper_equity` | strategy params (defaults are the validated ones) |

Edits require a restart to take effect (`bash stop.sh` → `bash start.sh`).

---

## Path to real money (deliberate — don't rush it)

1. **Paper now** — let the equity curve build for weeks.
2. If it's genuinely up with sane drawdowns over a real sample → add realistic
   shorting/funding costs and out-of-sample checks.
3. Only then: small real money on Binance (set keys in `.env`, set
   `exchange.testnet: false`), watched closely.

Never skip to real money on an unvalidated curve.

---

> The old per-symbol **smc** strategy (8 criteria, A/B/C tiers,
> `signal_only`/`approval`/`auto`) is **no longer active** — it had no proven
> edge. It remains as `engine: smc` for reference only; its settings default in
> code. The momentum engine doesn't use tiers, per-symbol signals, or those
> modes.
