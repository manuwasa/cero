# Setup

End-to-end walkthrough for a fresh machine. Plan ~30–60 minutes the first
time, mostly waiting on KYC. Subsequent runs are instant.

This guide assumes Windows + PowerShell. Linux/macOS steps are identical
except for the `uv` install command. Bash works the same.

---

## 1. Prerequisites

You need:
- **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/))
- **git** ([git-scm.com](https://git-scm.com/))
- **A bybit testnet account** (separate from mainnet; see step 4)
- **A Telegram account** (for alerts and approvals)
- **About 30 minutes** for first-time bybit KYC

Check Python:

```powershell
python --version    # should be 3.11+
```

---

## 2. Clone and install

```powershell
git clone <your-fork-url> cero
cd cero
```

We use [`uv`](https://github.com/astral-sh/uv) for dependency management. It
creates a `.venv` automatically and is much faster than pip.

### Install uv

Pick **one** of these. The official installer is cleanest because it
handles PATH for you; `pip install` is fine but you may need to fix PATH
manually after.

**Option A — official installer (recommended):**

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Installs to `%USERPROFILE%\.local\bin` and adds it to PATH automatically.
**Close and reopen PowerShell** before continuing.

**Option B — pip:**

```powershell
pip install --user uv
```

Installs to `%APPDATA%\Python\Python3xx\Scripts`, which Windows doesn't add
to PATH by default. See [Fixing PATH](#fixing-uv-on-path) below if `uv` isn't
found after install.

### Verify uv works

```powershell
uv --version
```

Should print something like `uv 0.11.16`. If you get
`'uv' is not recognized as the name of a cmdlet, function, script file...`,
PATH isn't set up — jump to [Fixing PATH](#fixing-uv-on-path).

### Install Cero's dependencies

```powershell
uv sync
```

Creates `.venv\` and installs everything from `pyproject.toml`. Takes a
minute the first time, instant after.

### Verify the install

```powershell
uv run python -c "from cero.config import load_config; print('OK')"
```

### Fixing uv on PATH

If `uv` isn't recognized after `pip install --user uv`, you have three
options:

**Quickest — just this terminal session:**

```powershell
$env:Path = "C:\Users\<you>\AppData\Roaming\Python\Python3xx\Scripts;$env:Path"
```

Replace `<you>` with your Windows username and `Python3xx` with your version
(e.g. `Python314`). Works until you close the window.

**Permanent — add to your user PATH** (no admin needed, one-time fix):

```powershell
[Environment]::SetEnvironmentVariable(
  "Path",
  "C:\Users\<you>\AppData\Roaming\Python\Python3xx\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User"),
  "User"
)
```

Close and reopen PowerShell. `uv` will work from any terminal forever after.

**Bypass — use the full path each time:**

```powershell
C:\Users\<you>\AppData\Roaming\Python\Python3xx\Scripts\uv.exe sync
C:\Users\<you>\AppData\Roaming\Python\Python3xx\Scripts\uv.exe run python -m cero
```

Verbose but works without touching PATH. Useful if you can't change system
settings.

> **Tip**: the official installer (Option A above) avoids this entire mess.
> If you went the pip route and PATH is a hassle, you can uninstall (`pip
> uninstall uv`) and reinstall via the official installer.

---

## 3. Configure secrets

Copy the template:

```powershell
copy .env.example .env
```

Open `.env` in your editor. You will fill three things:

- `EXCHANGE_API_KEY` + `EXCHANGE_API_SECRET` — bybit testnet keys (next step)
- `TELEGRAM_BOT_TOKEN` — from BotFather (step 5)
- `TELEGRAM_CHAT_ID` — your personal chat id (step 5)

Leave `EXCHANGE_PASSPHRASE` empty unless you're using OKX. **Never** commit
`.env` — it's already in `.gitignore`.

---

## 4. Bybit testnet — funds + KYC + API key

> The bybit setup is the most painful part of this guide and has the most
> ways to go wrong. Follow it carefully.

### 4a. Create your testnet account

1. Open [https://testnet.bybit.com](https://testnet.bybit.com)
2. Click **Sign up** (top right).
3. This is a **completely separate account** from mainnet. Even if you have
   a mainnet account with the same email, the testnet account is independent.
4. Verify your email.

### 4b. Complete KYC (Lite Verification / Lv1)

Bybit gates derivatives trading behind identity verification, **including on
testnet**. Skipping this means orders fail with `retCode: 10024`.

1. Top-right avatar → **Identity Verification**.
2. Choose **Lite Verification (Lv1)** — the basic tier.
3. You'll need:
   - A government-issued photo ID (passport, national ID, or driver's license).
   - A clear selfie.
4. Submit. Approval usually takes 5–15 minutes; sometimes up to a few hours
   during high load.

KYC status is shared between testnet and mainnet — verify once, both unlock.

Some regions (US, UK, Canada, parts of EU) are blocked from bybit derivatives
**regardless of KYC**. If after verification you still get retCode 10024,
your region is the issue and you'll need a different exchange. See the
[switching exchanges](#switching-exchanges) section below.

### 4c. Get testnet USDT (the faucet)

You need testnet USDT to do anything. Cero needs **at least a few hundred**
to compute sensible position sizes; 10,000 is the typical faucet allocation.

1. While logged in to testnet.bybit.com, visit
   `https://testnet.bybit.com/user/assets/home/overview`.
2. Look for **Request Test Coins** or **Receive Coins**. (Button location
   varies across UI versions; sometimes it's on the Assets page, sometimes on
   the API management page.)
3. Request USDT to the **Unified Trading Account** (UTA). **Only UTA is
   queryable via the bybit API** — funds in Spot or Funding accounts are
   invisible to Cero.
4. If you don't see the button, log out and back in. Some accounts only show
   it on a fresh session.
5. Confirm by going to the wallet overview — you should see USDT under
   "Unified Trading Account".

### 4d. Create an API key

1. Top-right avatar → **API**.
2. **Create New Key** → **System-generated API Keys**.
3. **Required permissions**:
   - ✅ **Read** (account, positions, orders)
   - ✅ **Unified Trading > Trade** (place/cancel orders)
4. **NEVER enable**:
   - ❌ Withdraw — this is non-negotiable; nothing Cero does requires it.
5. IP restriction: optional but recommended. Set to your home IP if static.
6. Copy the **API Key** and **API Secret** to `.env`:
   ```
   EXCHANGE_API_KEY=...
   EXCHANGE_API_SECRET=...
   ```
7. The secret is shown **once**. If you lose it, generate a new key.

### 4e. Verify the connection

```powershell
uv run python -c "
import asyncio
from cero.config import load_config
from cero.data.exchange import ExchangeClient

async def main():
    cfg, secrets = load_config()
    async with ExchangeClient(cfg, secrets) as ex:
        bal = await ex.fetch_balance()
        print(f'balance: {bal.equity} {bal.quote_currency}')

asyncio.run(main())
"
```

Expected output: `balance: 10000.0 USDT` (or whatever your faucet delivered).

If you see `balance: 0.0`: funds are in the wrong wallet. Transfer to Unified.

If you see `Could not contact DNS servers`: that's an aiodns issue on
Windows, already worked around in the code — see
[troubleshooting](#troubleshooting).

---

## 5. Telegram bot setup

### 5a. Create the bot

1. Open Telegram, find **@BotFather**, start a chat.
2. Send `/newbot`. Pick a name (display name, anything) and a username
   (must end in `bot`, e.g. `CeroPersonalBot`).
3. BotFather replies with a token like `8902455722:AAG...`. Copy it to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=8902455722:AAG...
   ```

### 5b. Find your chat ID

1. In Telegram, find **@userinfobot**, start a chat, send `/start`.
2. It replies with your numeric user ID (e.g. `8529944625`). Copy to `.env`:
   ```
   TELEGRAM_CHAT_ID=8529944625
   ```

### 5c. **Important: tap "Start" on your own bot**

Telegram bots cannot send messages to users who haven't first messaged the
bot. Search for your bot's username in Telegram, open the chat, **tap the
Start button** (or send any message). Skipping this means Cero will get
`Bad Request: chat not found` when trying to send alerts.

---

## 6. Review `config.yaml`

The defaults are sensible for learning. The fields most worth reviewing:

```yaml
exchange:
  name: bybit
  testnet: true        # KEEP TRUE until you've passed the validation gate
  leverage: 5          # max position size cap

symbols:
  - BTC/USDT:USDT
  - ETH/USDT:USDT
  - SOL/USDT:USDT

mode: signal_only      # KEEP signal_only at first; alerts only, no trades

risk:
  base_risk_per_trade_pct: 0.5    # risk 0.5% of equity per trade
  max_daily_loss_pct: 3.0         # auto-TRIP after losing 3% in one day
```

Don't change the criteria weights yet — they sum to 100 and a validator
will fail load if they don't. See `docs/CRITERIA.md` for what each does.

---

## 7. First run

```powershell
uv run python -m cero
```

If you get `'uv' is not recognized as the name of a cmdlet...`, PATH isn't
set up. See [Fixing uv on PATH](#fixing-uv-on-path) in step 2.

You should see:

```
INFO  Cero starting ...
INFO  db ready: data/cero.db
INFO  connected: 3426 markets loaded
INFO  telegram: connected as @YourBot (id=...)
INFO  price worker: started 18 streams (3 symbols x 6 timeframes)
INFO  scheduler: started (3 symbols, trigger=5m)
INFO  dashboard at http://127.0.0.1:8765
INFO  Cero up — exchange=bybit testnet=True mode=signal_only
```

In Telegram: you'll receive "Cero online — bybit (testnet), mode=signal_only".

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in a browser — you'll
see the dashboard with live balance, readiness per symbol, price chart, and
the news feed.

Press **Ctrl+C** to shut down. The shutdown logs a clean stop sequence.

---

## 8. What's next

- **Watch for a few hours** in `signal_only`. Alerts arrive when the brain
  scores a setup at tier C or higher. Compare them to what you see on the
  chart — does the criterion breakdown make sense?
- **Read `docs/USAGE.md`** for day-to-day operation: dashboard tour, Telegram
  commands, when to TRIP.
- **Read `docs/CRITERIA.md`** to understand exactly what each of the 8 checks
  measures and how the tier comes out.
- **Read `docs/VALIDATION.md`** for the 200-trade gate before you ever
  consider flipping `mode: auto`. The math is real — don't skip it.

---

## Troubleshooting

### `'uv' is not recognized as the name of a cmdlet, function, script file...`

PowerShell can't find `uv` because the install location isn't on PATH. This
is the most common first-time error. See
[Fixing uv on PATH](#fixing-uv-on-path) in step 2 for three fixes (quick
one-shot, permanent, or full-path bypass).

### Cero tripped on its own trade (`unexpected_position` with `exch_id=None`)

If you see a TRIP fire with a detail like
`unexpected position BTC/USDT:USDT short size=-0.021 (exch_id=None)` right
after Cero placed an order itself: this **was** a real bug — `orders.py`
wrote the order id into `exchange_position_id`, but bybit's
`fetch_positions` returns `None` for that field. The `account_worker`
reconciliation then saw the same position as "new" on the next poll and
tripped. **Fixed** in [cero/exec/orders.py](../cero/exec/orders.py#L209).
Pull the latest code. See `docs/USAGE.md` for full recovery steps.

### `bybit GET .../instruments-info?category=option&baseCoin=...` failing

By default ccxt's bybit `load_markets` fetches spot + linear + inverse +
**option** market metadata. Options endpoints on bybit testnet are flaky
and often time out, which makes the whole boot fail with a retry loop.
Cero limits `fetchMarkets` to `["linear"]` (USDT perps, all we trade) so
this no longer happens. If you see it on a fresh checkout, pull the latest.

### `429 Too Many Requests` from FairEconomy (`nfs.faireconomy.media`)

The calendar feed throttles aggressive polling. If you restart Cero many
times in an hour during development, the boot-time fetch hits the limit.
Cero now skips the initial fetch on boot if it already has data younger
than 30 minutes (see `min_refresh_gap_seconds` in
[cero/data/calendar_worker.py](../cero/data/calendar_worker.py)). If you
hit a 429 anyway, the worker backs off (30→60→90→120s → cap at 15 min)
and keeps the rest of Cero running. Wait an hour and it self-recovers.

### `Could not contact DNS servers`

`aiodns` (a ccxt + aiogram dependency) sometimes can't auto-detect DNS
servers on Windows. Cero injects an `aiohttp.ThreadedResolver` to work around
this — if you ever see this error, the fix is already in
`cero/data/exchange.py` and `cero/ui/telegram/bot.py`. Pull the latest code.

### `retCode: 10024` from bybit on order placement

KYC not completed, or your region is blocked. Re-run KYC (step 4b). If still
blocked, see [switching exchanges](#switching-exchanges).

### `Bad Request: chat not found` from Telegram

You haven't tapped Start on your bot yet. See step 5c.

### `balance: 0.0` from Cero but bybit UI shows funds

Funds are in Spot or Funding, not Unified. On bybit testnet, **only Unified
is queryable via API**. Transfer them: Assets → Transfer → Spot/Funding →
Unified Trading.

### Numbers display as `1.234,56` instead of `1,234.56`

That's your system locale (Indonesian, German, etc.). JavaScript's
`toLocaleString()` picks it up automatically. No bug — culture-specific
formatting.

### Cero starts but the dashboard is unreachable

Two checks:
- Are you accessing `http://127.0.0.1:8765` (not `https://`)?
- Is something else already on port 8765? Change `web.port` in `config.yaml`.

### Switching exchanges

If bybit is blocked in your region, Cero supports any ccxt exchange. Edit
`config.yaml`:

```yaml
exchange:
  name: okx          # or 'binance', 'hyperliquid'
  testnet: true
```

OKX needs `EXCHANGE_PASSPHRASE` in `.env`; others ignore it. You may need
small per-exchange tweaks (the `fetchCurrencies: False` knob in
`cero/data/exchange.py` is bybit-specific) — open `docs/DEVELOPMENT.md` for
how to add an exchange.
