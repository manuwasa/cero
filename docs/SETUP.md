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

Defaults are tuned for the momentum engine. The fields worth a look:

```yaml
exchange:
  name: binance          # your TRADING venue (for real orders, later)
  data_exchange: bybit   # where CHART DATA comes from — must be reachable from
                         # wherever Cero runs. Public data, no keys needed.
  testnet: true          # keep true; momentum is paper for now

engine: momentum         # the active strategy (daily long/short momentum)

momentum:
  auto_universe: true    # auto-pick the most-liquid coins (no fixed list)
  universe_size: 50
  rebalance_days: 5
```

For **paper you need no API keys** — chart data is public. Keys (in `.env`)
only matter when you eventually trade real money on `name`.

---

## 7. First run

```bash
python -m cero            # with the venv active
# or:  uv run python -m cero
```

A healthy momentum boot:

```
connected: NN data markets — data from bybit, orders via binance
momentum engine started — universe: auto (top-50 liquid), rebalance 5d, equity 10000
Cero up — engine=momentum (daily long/short paper)
auto-universe: NN liquid perps
[MOM] equity 10000 (+0.0%) day +0 | 13L/13S  REBALANCED
```

Seeing `connected … data from bybit` and `[MOM] … REBALANCED` means it works.
Telegram (if configured): "Cero online — momentum engine (paper, NN coins)".
Dashboard: [http://127.0.0.1:8765](http://127.0.0.1:8765). **Ctrl+C** to stop.

If boot fails on the data load, Bybit isn't reachable from this network — run
where it is (home wifi / VPN), or change `data_exchange`.

---

## 8. What's next

- **Let it run (paper)** and glance daily: `scripts/momentum_check.py`, or
  Telegram `/status` + `/book`.
- **Run it 24/7 on your phone + deploy updates:** see
  [`docs/USAGE.md`](USAGE.md) — the full run / deploy / operate guide.
- It's a **modest, paper** edge — judge it over weeks, not days. Real money is a
  deliberate later step (see USAGE.md).

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
