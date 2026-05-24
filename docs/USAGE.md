# Usage

Day-to-day operation: starting Cero, reading the dashboard, talking to the
Telegram bot, deciding when to TRIP, and how to graduate from `signal_only`
to `approval` to `auto`.

If you haven't done first-time setup, start with [`docs/SETUP.md`](SETUP.md).

---

## Starting and stopping

```powershell
uv run python -m cero
```

Boot takes ~5 seconds. You'll see initialization logs in your terminal, a
"Cero online" message in Telegram, and the dashboard at
[http://127.0.0.1:8765](http://127.0.0.1:8765).

**Ctrl+C** to shut down — Cero unwinds every worker cleanly, sends a
"shutting down" notice to Telegram, and saves any pending DB writes. Avoid
killing the process forcibly (Task Manager → End Task) because in-flight
orders may not finish recording.

---

## The three modes

`mode` in `config.yaml` controls what Cero does when a signal fires.

| Mode | Behavior | Order placer |
| --- | --- | --- |
| `signal_only` | Alert only, never trades | `StubOrderPlacer` (no network calls) |
| `approval` | Asks via Telegram ✅/❌ buttons, 60s timeout | `CcxtOrderPlacer` (real) |
| `auto` | Trades A/B-tier signals immediately | `CcxtOrderPlacer` (real) |

**Start in `signal_only`.** Watch for a few weeks. Compare signals to what
you see on the chart. Only consider `approval` once you have a feel for when
the brain is right vs wrong.

Mode hot-swap at runtime is not yet wired (it's an open enhancement). For
now, edit `config.yaml` and restart.

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
