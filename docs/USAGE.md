# Usage

Day-to-day operation: starting Cero, reading the dashboard, talking to the
Telegram bot, deciding when to TRIP, and how to graduate from `signal_only`
to `approval` to `auto`.

If you haven't done first-time setup, start with [`docs/SETUP.md`](SETUP.md).

---

## ⚠️ Before you trust anything Cero says

Cero's **code** has been tested and works end-to-end. Cero's **strategy** —
the 8-criteria scoring in [`docs/CRITERIA.md`](CRITERIA.md) — has **not**
been proven profitable on current crypto markets. It's based on respected
SMC/ICT concepts but never validated empirically.

That means:

- The win rate you see when you boot Cero for the first time means **nothing**
  until your signal sample reaches ~30 (suggestive) and then ~200 (reliable).
- "Cero placed an order and lost" after 5 trades is statistical noise, not
  proof of failure.
- "Cero placed 10 winners in a row" after 10 trades is also statistical
  noise, not proof of success.

The right way to use Cero, especially if you're not an experienced trader:

1. **`mode: signal_only`** for at least 4 weeks. No real money or testnet
   money is placed; signals just accumulate in the DB.
2. **Run [`scripts/morning_check.py`](../scripts/morning_check.py) once a
   day** — 30-second daily glance at where you stand.
3. **Run [`scripts/backtest_signals.py`](../scripts/backtest_signals.py)
   weekly** — full validation-gate stats against accumulated signals.
4. **When the gate passes** (200+ signals, ≥55% WR, PF ≥1.5, stable),
   consider `approval`. Then `auto`. Each step earns the next.

If at any point the data says "the strategy doesn't have edge" — accept
that. Either rework the criteria (research, not guesswork), pick a
different strategy, or stop. Don't go to mainnet hoping it'll be different.

See [`docs/VALIDATION.md`](VALIDATION.md) for the full statistical
reasoning. The math is unforgiving but the discipline saves accounts.

---

## Starting and stopping

```bash
python -m cero            # with the venv active
# or:  uv run python -m cero
```

Boot takes ~5 seconds. Watch for two confirmation lines: `market data from
mainnet` (real prices — see [Market data](#market-data-comes-from-mainnet))
and `mode=… ` (your execution mode). You'll also get a "Cero online" message
in Telegram and the dashboard at [http://127.0.0.1:8765](http://127.0.0.1:8765).

**Ctrl+C** to shut down — Cero unwinds every worker cleanly, sends a
"shutting down" notice to Telegram, and saves any pending DB writes. Avoid
killing the process forcibly because in-flight orders may not finish recording.

### One-time: start on clean data

Cero reads market data from **mainnet** even on testnet (testnet OHLCV is
unreliable — SOL once printed 9,900). If your `data/cero.db` was filled by an
older build that stored testnet candles, clear it once so the brain starts on
real prices (Cero rebuilds it on boot):

```bash
mv data/cero.db data/cero_testnet_old.db   # keeps a backup
```

### Running 24/7 on a phone (Termux) — watchdog

Termux may sleep or kill a long-running process. Two small helper scripts in
the project root keep Cero alive: `start.sh` launches it and **auto-restarts**
on any crash/exit; `stop.sh` stops it cleanly without restarting. They live
only on the device (not tracked in git) — **this doc block is the canonical
backup; recreate them from here if the device is reset.**

`start.sh`:
```bash
#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python"   # use the venv automatically
mkdir -p logs; rm -f .cero_stop; echo $$ > .cero_watchdog.pid
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
trap 'rm -f .cero_watchdog.pid; command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock' EXIT
while [ ! -f .cero_stop ]; do
  echo "$(date '+%F %T') starting cero" | tee -a logs/watchdog.log
  "$PY" -m cero
  [ -f .cero_stop ] && break          # clean stop requested → don't restart
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
touch .cero_stop                              # tell start.sh's loop to exit, not restart
pkill -f "python -m cero" 2>/dev/null         # stop the bot (clean shutdown)
[ -f .cero_watchdog.pid ] && kill "$(cat .cero_watchdog.pid)" 2>/dev/null
echo "stop signal sent."
```

Run with `bash start.sh` / `bash stop.sh`. To survive closing the session,
start inside tmux: `tmux new -s cero` → `bash start.sh` → detach with
**Ctrl+b** then **d**. Reattach later with `tmux attach -t cero`.

### Deploying an update from your laptop

Cero runs on the phone, but you edit and commit on the laptop. To push a change
live: SSH into the phone, then **stop → pull → start**:

```bash
ssh u0_a585@192.168.0.3 -p 8022      # your phone's Termux SSH: user@ip -p port
source ~/cero/.venv/bin/activate     # optional for start/stop; handy for scripts
cd ~/cero
bash stop.sh
git pull
bash start.sh
```

Notes:
- `u0_a585@192.168.0.3 -p 8022` are *your* device's SSH details. The IP changes
  when the phone reconnects to Wi-Fi — check it with `ip addr` on the phone.
- **Do NOT wipe the DB on a normal restart.** Only clear it for a fresh start or
  after a fix that corrupted stored data (rare), and only once:
  ```bash
  mv data/cero.db data/cero_testnet_old.db   # ONE-TIME clean slate, then start.sh
  ```
  Wiping every restart deletes your paper history, so the strategy never gathers
  enough trades to judge.

---

## The three modes

`mode` in `config.yaml` controls what Cero does when a signal fires.

| Mode | Behavior | Order placer |
| --- | --- | --- |
| `signal_only` | Alert only, never trades | `StubOrderPlacer` (no network calls) |
| `paper` | Trades on PAPER vs live real prices — simulated fills, real PnL, **no money** | `PaperBroker` (no exchange orders) |
| `approval` | Asks via Telegram ✅/❌ buttons, 60s timeout | `CcxtOrderPlacer` (**real money**) |
| `auto` | Trades A/B-tier signals immediately | `CcxtOrderPlacer` (**real money**) |

**`paper` is the validation workhorse.** It runs the whole bot on real prices
and tracks simulated PnL + equity with every risk gate armed (daily-loss cap,
consecutive-loss TRIP, position limits) — so you learn whether a strategy
actually makes money *before* a cent is at risk. Set `paper_equity` in
`config.yaml` for the simulated account size.

**Progression — earn each step:** `signal_only` → `paper` (until a strategy
is profitable in paper over a meaningful run) → `approval` (small real money,
you confirm each trade) → `auto`. Never skip to real money on an unvalidated
strategy.

Mode hot-swap at runtime is not yet wired (it's an open enhancement). For
now, edit `config.yaml` and restart.

---

## Market data comes from mainnet

Cero pulls **candles and tickers from mainnet** even when `exchange.testnet:
true`, because testnet OHLCV is unreliable — illiquid books produce fantasy
wicks (SOL has printed 9,900 and frozen at 1,372 while real SOL was ~$80),
which silently poisons every signal. Trading orders still route to whatever
`testnet` selects, so you keep a safe order venue while the brain reasons on
real prices.

Controlled by `exchange.market_data_testnet` in `config.yaml` (default
`false` = mainnet data; no API keys needed — it's public). The boot log
prints `market data from mainnet` (or `testnet`) so you can confirm at a
glance.

---

## The dashboard tour

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) while Cero is running.

### Top row

- **Account** — live equity, balance, unrealized PnL, margin used. The
  `source` field shows `exchange` (live fetch) or `cached` (last
  AccountSnapshot row, fallback when the network is flaky).
- **PnL** — today's realized PnL + win/loss split, and all-time totals.
  Resets at UTC midnight.
- **Controls** — TRIP button (red) cancels every open order and closes every
  position at market. Reset button clears an active TRIP. Both have
  confirm-dialogs.

### Readiness table

One row per configured symbol. Columns:

- **tier** — A (green), B (amber), C/D (muted). Updated on each closed 5m bar.
- **direction** — long / short / none.
- **score** — out of 100. See `docs/CRITERIA.md` for the math.
- **age** — how long since this score was computed. On testnet you may see
  large ages (10+ minutes) when 5m bars don't close because the market is
  quiet.

### Equity card

Green line chart of your account equity over the selected window (6h to
30d). Right side shows current equity + change vs the start of the window.
Useful for spotting drawdowns at a glance — once you have meaningful trade
history, this is the first thing to check each morning.

### Price card

Pink line chart of the selected symbol + timeframe. Defaults to BTC 1h.
The symbol and timeframe dropdowns rebuild the chart immediately. Last 120
bars by default.

### Positions

Currently-open positions reconciled from the exchange every 10 seconds by
`account_worker`. Side colored green/red. uPnL colored too. SL/TP shown if
the position has brackets (which it always does when placed by Cero).

### News

Latest 15 headlines from configured RSS feeds (default: CoinTelegraph +
Reddit r/cryptocurrency). Refreshed every 15 minutes. Click any headline to
open in a new tab. **News does not gate trading** — it's context.

### Live events

Real-time event log via WebSocket. Two topics are currently piped:
- `signal:new` — fired when the brain emits a tier A/B/C signal
- `trip:fired` — fired when TRIP triggers (manual or automatic)

Each entry shows time, topic, and payload. Capped at 50 entries.

---

## Telegram commands

Send these to your bot in Telegram. The bot ignores messages from anyone
except `TELEGRAM_CHAT_ID` (and optionally `TELEGRAM_CHAT_ID_2`).

| Command | What it does |
| --- | --- |
| `/start` | Greeting + pointer to `/help` |
| `/help` | List of commands |
| `/status` | Mode, exchange, testnet flag, symbols, trip state |
| `/readiness` | Latest tier + direction + score per symbol |
| `/positions` | All open positions |
| `/pnl` | Today's PnL + all-time totals |
| `/trip [reason]` | Fire the kill switch with optional explanation |
| `/reset` | Clear the active TRIP, allow trading again |
| `/trips` | Last 10 trip events (active + cleared) |

Approval-mode signals arrive with **✅ Approve / ❌ Reject** buttons. Tap
within 60 seconds (configurable via `approval_timeout_s` when constructing
the mode). After timeout or rejection, the trade is skipped silently.

---

## Understanding signals

When the brain finishes evaluating a symbol, it logs and (sometimes) emits
a Signal. Anatomy of a signal:

```
ETH/USDT:USDT  📈 long  Tier B  •  score 67/100  •  ✅ actionable

entry      3000.00
stop       2920.00      (1× ATR(H1) away, clamped to 0.3–3% of price)
target     3160.00      (2× stop distance, 2:1 R:R)
size       0.3125        (×0.5 = tier-B multiplier)

(ok — passed all gates)
```

- **Tier** comes from the score-to-tier mapping in `risk.tier_thresholds`.
- **Direction** comes from criterion 1 (HTF trend on H1+H4).
- **Size** is `(equity * base_risk_pct * tier_multiplier) / stop_distance`.
  That last division means: if SL hits, you lose exactly `base_risk_pct *
  tier_multiplier` percent of equity. The size scales with the stop distance.
- **size_reason** at the bottom explains why size is what it is. Common
  reasons besides "ok":
  - `tier sizing is 0 (C or D)` — not actionable, tier too low
  - `daily loss X% >= cap Y%` — daily loss cap hit
  - `max concurrent positions (N) already open`
  - `news blackout: <event name>`
  - `TRIPPED (<reason>): <detail>`

Signals are persisted to the `signals` table regardless of whether they're
actionable. You can review history in the DB or via
`/api/readiness/{symbol}`.

---

## When to TRIP

The kill switch. Use it whenever something feels off. Specific cases:

- **You see an unexpected position** in `/positions` — Cero will TRIP itself
  if its account_worker detects a manual trade, but if you spot it first,
  hit TRIP and investigate.
- **The exchange behaves weirdly** — partial fills, weird prices, slow API.
- **The brain is firing signals that look obviously wrong** — better to halt
  and read the criteria breakdown than to let bad trades stack.
- **You're about to be away from your machine** during high-impact news.
  TRIP, then `/reset` when you're back.

TRIP triggers automatically when:
- Daily realized PnL is below `-max_daily_loss_pct` (default 3%).
- Consecutive losses reach `max_consecutive_losses` (default 4).
- An unexpected position appears on the exchange.

What TRIP does:
1. Sets the gate to `tripped=True` in `RiskGate`.
2. Inserts a `TripEvent` row with reason + detail.
3. Publishes `trip:fired` on the bus.
4. `TripWatcher` (subscribed to that topic) cancels every open order via
   `placer.cancel_all_for(symbol)` and closes every open position via
   `placer.close_position(symbol)`, for every configured symbol.
5. Telegram sends a `TRIPPED` notice.
6. The dashboard shows a red banner.

`/reset` un-trips. **Never auto-resets** by design — you decide when it's
safe to resume.

---

## Adding a symbol

Three steps, plus two helper scripts to find the right values.

### 1. Find a valid symbol on your exchange

Not every coin on bybit mainnet is on testnet — testnet has a smaller list.
Use the helper to search:

```powershell
uv run python scripts/list_symbols.py --grep DOGE
```

Outputs the matching ccxt-unified format symbols. For bybit USDT-margined
perps the format is always `BASE/USDT:USDT`, e.g. `DOGE/USDT:USDT`. If
`--grep <coin>` returns nothing, that coin isn't traded on the configured
exchange — pick a different symbol or switch exchange.

Other ways to confirm a symbol exists:
- Bybit web UI: https://testnet.bybit.com/trade/usdt/BTCUSDT (browse the
  symbol picker). The URL slug tells you the underlying name.
- `https://api-testnet.bybit.com/v5/market/instruments-info?category=linear`
  returns the full JSON listing.

### 2. Find the round-number step

Criterion 3 needs to know what counts as a "psychological round price"
for that asset. The helper computes it from the current price:

```powershell
uv run python scripts/suggest_round_step.py DOGE/USDT:USDT
```

Output is a ready-to-paste dict snippet for `_ROUND_STEPS` in
[cero/brain/scheduler.py](../cero/brain/scheduler.py). Example:

```
symbol                      price     step (~0.5%)
BTC/USDT:USDT             71647.1              500
DOGE/USDT:USDT            0.05975           0.0002
```

The step is sized at ~0.5% of price and snapped to a clean
`1 / 2 / 5 × 10^N` value — that's where humans cluster round-number levels.

If you skip this and don't add an entry, the symbol falls back to `1000.0`,
which is correct for BTC and meaningless for anything else (criterion 3
will always fail).

### 3. Wire it up

Edit [config.yaml](../config.yaml):

```yaml
symbols:
  - BTC/USDT:USDT
  - ETH/USDT:USDT
  - SOL/USDT:USDT
  - DOGE/USDT:USDT     # new
```

Edit `_ROUND_STEPS` in [cero/brain/scheduler.py](../cero/brain/scheduler.py):

```python
_ROUND_STEPS: dict[str, float] = {
    "BTC/USDT:USDT": 1000.0,
    "ETH/USDT:USDT": 100.0,
    "SOL/USDT:USDT": 10.0,
    "DOGE/USDT:USDT": 0.0002,   # new (from suggest_round_step.py)
}
```

Restart Cero. You'll see the new symbol's 6 timeframes backfill on boot
(1,800 candles added) and the readiness table on the dashboard grows by
one row.

### How many positions can Cero hold at once?

`max_concurrent_positions: 3` in `config.yaml` is the cap across **all
symbols combined**. Each symbol can only hold one position at a time —
Cero runs in bybit's one-way mode, so you can't be long and short the same
symbol simultaneously. Sequential trades (close BTC long → open BTC short)
work fine; parallel trades on different symbols (BTC + ETH + SOL all open)
also work fine, up to the cap.

Bump the cap if you trade many symbols:

```yaml
risk:
  max_concurrent_positions: 6
```

Higher caps = more parallel risk. Don't go above the number of symbols you
configured, and don't go above what you can mentally track.

---

## Recovering from a trip

Whether the trip was you (manual `/trip`), an automatic trigger (daily
loss cap, consecutive losses), or `unexpected_position`, the recovery is
the same three-step flow.

### 1. Verify exchange state is what you expect

Open the exchange's web UI (e.g. https://testnet.bybit.com → Positions).
TripWatcher should have closed everything, but trust nothing automatically:
- **If positions are flat**: continue to step 2.
- **If a position is still open**: close it manually on the exchange before
  doing anything else. The DB and the exchange must agree before you reset.

### 2. Clean up stale DB rows (only if needed)

If positions are flat on the exchange but Cero's `positions` table still
has rows, they're orphans. Inspect:

```powershell
uv run python -c "
import asyncio
from sqlalchemy import select
from cero.config import load_config
from cero.db.models import Position
from cero.db.session import close_db, init_db, session_factory

async def main():
    cfg, _ = load_config()
    await init_db(cfg.database)
    async with session_factory()() as s:
        rows = (await s.execute(select(Position))).scalars().all()
        for r in rows:
            print(f'  id={r.id}  {r.symbol}  {r.side}  size={r.size}')
        print(f'total: {len(rows)} rows')
    await close_db()

asyncio.run(main())
"
```

If the rows don't match anything live on the exchange, clear them via the
account_worker on the next boot — it'll detect them as "closed" and delete.
Or, faster, run the script again with `await s.execute(delete(Position))`.

### 3. Reset the trip

- **In Telegram**: send `/reset` to your bot.
- **In the dashboard**: click the **Reset** button.

Both update in-memory `RiskGate.tripped` and the DB row in one call.

If neither is available (e.g. Cero crashed), you can clear the DB row only —
but **remember to restart Cero afterward** so `hydrate()` re-reads the (now
cleared) state:

```powershell
uv run python -c "
import asyncio, time
from sqlalchemy import update
from cero.config import load_config
from cero.db.models import TripEvent
from cero.db.session import close_db, init_db, session_factory

async def main():
    cfg, _ = load_config()
    await init_db(cfg.database)
    async with session_factory()() as s:
        result = await s.execute(
            update(TripEvent).where(TripEvent.cleared_at.is_(None))
            .values(cleared_at=int(time.time() * 1000), cleared_by='manual')
        )
        await s.commit()
        print(f'cleared {result.rowcount} trip(s)')
    await close_db()

asyncio.run(main())
"
```

### Confirming the gate is clear

After reset, the next signal log line's reason changes from
`TRIPPED (...)` back to the normal sizing reason
(`tier sizing is 0 (C or D)`, `ok`, etc.). That's your confirmation.

---

## What's exact, what's approximate

The numbers Cero shows on `/pnl`, the dashboard, and the `trades` table are
**accurate enough for the 200-trade validation gate** but **not for tax or
cent-accurate accounting**. Worth knowing exactly what's approximated:

### Exact

- **Trade count** — every position that opens then closes writes one Trade row.
  The count you see is the count that happened.
- **Win/loss sign** — `realized_pnl > 0` is a win, `< 0` is a loss. Sign is
  always correct.
- **Direction (long/short)** — recorded exactly from the Signal that opened it.
- **Entry price (intended)** — the price the brain saw when it built the signal.
- **Size** — the *actual filled amount* on the exchange, including partial fills
  (e.g. requested 12 SOL, only 1.7 filled → DB shows 1.7).
- **Stop loss / take profit** — exact, since they're the bracket params we sent.

### Approximate

- **`Trade.entry_price`** — uses the *intended* entry from the Signal, not the
  actual exchange fill price. On testnet (thin liquidity) and during volatile
  moments, slippage can be 0.1–0.5%. Mainnet on top-volume pairs: usually <0.05%.
- **`Trade.exit_price`** — uses the *last `mark_price` we observed* before the
  position disappeared. The real fill might have been at SL or TP, which is
  typically within a few ticks of the mark but not identical. Same magnitude of
  error as entry.
- **`Trade.realized_pnl`** — computed from the two approximate prices above
  (`(exit - entry) × size`, sign flipped for shorts). Magnitude is roughly
  right; expect 0.1–1% error vs. bybit's actual reported P&L.
- **`Trade.exit_reason`** — always `'other'`. We don't currently distinguish SL
  hit / TP hit / manual close / liquidation. Fixing this requires a
  `fetch_closed_orders` follow-up pass in `account_worker`, deferred.
- **`Trade.fees`** — always `0.0`. We don't track exchange fees. Bybit charges
  ~0.06% taker fee on perps, so a $1,000 trade pays $0.60 each way ($1.20 round
  trip). Over 200 trades that's ~$240 of unaccounted fees on a $10k account.

### What this means in practice

For the validation gate (`docs/VALIDATION.md`):
- **Trade count, win rate, profit factor** → calculated from the *signs* of
  `realized_pnl`. These are reliable.
- **Average win / average loss / max drawdown** → magnitudes are 0.1–1% off.
  Not enough to change a "pass" into a "fail" or vice versa unless you're right
  on the edge.

For real-world accounting:
- **Always cross-reference with bybit's official "Trade History" or
  "Closed P&L" export** before any tax filing or capital decision.
- Cero's numbers are for *operational visibility* (is it working? am I on
  pace?), not *financial truth*.

If/when you need cent-accurate numbers, the fix is to extend `account_worker`
to call `exchange.fetch_closed_orders(symbol)` whenever it detects a position
closure, and rewrite the Trade row's exit_price/realized_pnl/exit_reason/fees
from that response. ~50 lines of work; deferred until validation actually
needs it.

---

## Backtesting accumulated signals

`scripts/backtest_signals.py` answers the question "if we had taken every
tier-A/B signal at its stated entry/SL/TP, what would the win rate be?"

It walks forward through 5-minute candles after each signal and finds whether
SL or TP was hit first. No real orders are placed.

```powershell
# default: tier A+B, 24h horizon, all symbols
uv run python scripts/backtest_signals.py

# narrow to one symbol, longer horizon
uv run python scripts/backtest_signals.py --symbol ETH/USDT:USDT --horizon-hours 48

# only tier B
uv run python scripts/backtest_signals.py --tier B
```

Output gives you win rate, profit factor, max consecutive losses, stability
(first-half vs last-half WR), per-tier + per-symbol breakdown, and an explicit
**validation-gate check** against the four thresholds from
[docs/VALIDATION.md](VALIDATION.md):
- count >= 200
- win rate >= 55%
- profit factor >= 1.5
- stability (first half vs last half within 5 percentage points)

### What it doesn't model (yet)

- **Slippage** — real fills are 0.05–0.5% off the brain's intended entry
- **Fees** — ~0.06% per leg on bybit perps, missed in the backtest
- **Partial fills** — IOC cancellation on thin liquidity (testnet especially)

Real auto-mode performance will be **0.2–1% worse per trade** than what the
backtester shows. Treat the backtest as an honest upper bound, not a forecast.

### Why this is the right validation tool

You don't need to actually run `auto` mode to know whether the strategy
works. Every tier-A/B signal Cero emits in **any mode** (signal_only,
approval, auto) gets persisted to the `signals` table with the brain's
intended entry/SL/TP. The backtester computes outcomes from those + the
candle history — same answer you'd get from auto-mode trading minus the
slippage/fees you'd actually pay.

Run it daily/weekly as your signal sample grows. Once it shows gate-passing
stats over 200+ signals, you have evidence the brain's edge is real, and
auto mode becomes a reasonable next step.

---

## Reading the validation gate

Before flipping `mode: auto`, you need to pass all five tests in
`docs/VALIDATION.md`:

1. ≥ 200 trades
2. Win rate ≥ 55% sustained
3. Profit factor ≥ 1.5
4. Stability: first-100-WR within 5% of last-100-WR
5. Max drawdown under your tolerance

Run `/pnl` in Telegram or check the dashboard regularly. Cero doesn't yet
auto-compute "gate status" — that's an open enhancement. For now, manually
verify each test before scaling.

---

## Operational rules

A few habits that catch most accidents:

- **Don't run two Ceros at once.** Same API key, same symbols, double-trades.
- **Don't edit `config.yaml` while Cero is running.** Stop, edit, restart.
- **Watch the equity chart, not the price chart.** The job is making the
  green line go up, not predicting BTC.
- **`logs/cero.log`** rotates at 50 MB. If you ever need to debug a past
  signal, `grep` is your friend.
- **Backup `data/cero.db`** before any major config change. Trade history is
  there; you don't want to lose 50 trades worth of validation data.

---

## Mainnet checklist

When you're about to flip `testnet: false` for the first time:

- [ ] Passed all 5 validation gates on testnet (200+ trades)
- [ ] **Generated a new mainnet API key** with Read + Trade only, NO Withdraw
- [ ] **IP whitelisted** the API key
- [ ] **Funded with the smallest amount you can stand to lose** (Stage 1 of
      `docs/VALIDATION.md` — $50–$100)
- [ ] `mode: signal_only` for the first session even on mainnet — sanity-check
      that signals look the same with mainnet data
- [ ] Logged into Telegram on your phone (not just desktop) so you get alerts
      even when away from the computer
- [ ] Re-read `docs/VALIDATION.md`

After your first mainnet trade closes, sit on it for at least 24 hours
before deciding anything. The first trade always feels different.
