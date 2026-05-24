# Deploy Cero to an Android phone (Termux)

The cheapest way to run Cero 24/7 without paying for cloud hosting or
needing a credit card. An old Android phone, a charger, and home WiFi
is all you need.

This guide takes ~45 minutes end-to-end the first time.

---

## What you need

- **An Android phone**, Android 7+, 2GB+ RAM. An old phone you don't use
  daily is ideal.
- **A charger** that stays plugged in. Phone runs on AC power — battery
  becomes a backup against power blips.
- **Home WiFi** the phone can stay connected to.
- **A laptop or desktop** with the Cero repo (for initial setup + ongoing
  access). Phone runs the bot; laptop is for monitoring.

## What you get

- Cero runs continuously
- All accumulated data (signals, candles, trades) survives reboots
- Dashboard reachable from any device on your home WiFi (with password)
- Telegram bot works as normal
- **$0/month**

---

## 1. Install Termux from F-Droid

> Do **not** install Termux from the Google Play Store. The Play version
> is years out of date and many packages won't work.

1. On the phone, open https://f-droid.org/ and install **F-Droid** itself
2. In F-Droid, search and install all three:
   - **Termux** (the terminal)
   - **Termux:API** (lets the terminal access phone features)
   - **Termux:Boot** (auto-start Cero on phone reboot)

When you open Termux you'll see a Linux-style shell.

## 2. Initial Termux setup

Inside Termux, run:

```bash
pkg update && pkg upgrade -y
termux-setup-storage           # tap Allow on the storage prompt
pkg install -y python git rust libffi openssl-tool nano
```

`rust` is needed because some Python packages compile native code on ARM.

## 3. Install uv

```bash
pip install --user uv
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
uv --version
```

Should print `uv 0.x.y`. If "uv not found", re-source `.bashrc` or close
and reopen Termux.

## 4. Clone Cero

You need the code on the phone. Easiest path is via GitHub:

```bash
git clone https://github.com/<your-user>/<your-repo>.git cero
cd cero
```

If your repo is private, you'll need to either make it public temporarily,
or set up an SSH key / personal access token.

## 5. Install dependencies

```bash
uv sync
```

This takes 3–5 minutes on ARM (longer than a laptop because some packages
build from source). Don't worry about the warnings unless one is an error.

If a package fails to build with a "missing C header" error, install the
matching native package:

```bash
pkg install <missing-name>
uv sync
```

Common ones: `libxml2`, `libxslt`, `clang`.

## 6. Configure credentials

```bash
nano .env
```

Paste your bybit testnet keys + Telegram credentials. Save with
`Ctrl+O`, `Enter`, `Ctrl+X`.

```env
EXCHANGE_API_KEY=your_testnet_key
EXCHANGE_API_SECRET=your_testnet_secret
EXCHANGE_PASSPHRASE=
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## 7. Enable dashboard auth (REQUIRED on LAN)

`config.yaml` defaults to `web.host: 127.0.0.1` which means only the phone
itself can see the dashboard. To reach it from your laptop on the same
WiFi, bind to `0.0.0.0` — but **only after** setting credentials.

Edit `config.yaml`:

```yaml
web:
  host: 0.0.0.0       # bind on all interfaces — LAN reachable
  port: 8765
  auth_user: "cero"             # pick anything
  auth_pass: "long-random-string"   # pick something long
```

Pick a password you'd be OK with everyone on your home WiFi seeing the
dashboard if they guessed it. Length matters more than complexity — 20+
random characters is fine. A passphrase ("apple-truck-honest-yellow-42")
works too.

If you keep `host: 127.0.0.1`, you don't need auth, but then the dashboard
isn't reachable from any other device. Telegram + SSH stay available.

## 8. Test it runs

```bash
uv run python -m cero
```

You should see:
```
INFO  Cero starting ...
INFO  db ready: data/cero.db
INFO  connected: 3426 markets loaded
INFO  telegram: connected as @YourBot
INFO  price worker: started ...
INFO  scheduler: started ...
INFO  dashboard at http://0.0.0.0:8765
INFO  Cero up — exchange=bybit testnet=True mode=signal_only
```

In your Telegram chat with the bot, you should get "Cero online".

`Ctrl+C` to stop. We'll set up auto-start next.

## 9. Stop Android from killing Cero

Two settings matter for 24/7 operation.

**a) Wake lock.** Keeps the CPU running even when the screen is off:

```bash
termux-wake-lock
```

This survives Cero crashes; it's a Termux-wide setting until released.

**b) Battery optimization.** Android tries to be helpful by killing
"background apps" — including Termux. You must whitelist Termux:

- **Settings → Apps → Termux → Battery → Unrestricted**
  (also called "Don't optimize" or "Allow background activity"
  depending on phone manufacturer)
- Do the same for **Termux:Boot**

This is the single most common reason Cero stops working overnight.
Don't skip it.

## 10. Auto-start Cero on phone boot

If the phone reboots (battery dies, OS update, etc.), you want Cero to
restart automatically.

Termux:Boot reads scripts from `~/.termux/boot/`. Create one:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-cero <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/cero
exec uv run python -m cero
EOF
chmod +x ~/.termux/boot/start-cero
```

Test it by rebooting the phone. Within ~30 seconds of boot, Cero should
be running. Check Telegram for the "Cero online" message.

## 11. Access the dashboard from your laptop

Find the phone's local IP:

```bash
ifconfig wlan0 | grep "inet "
```

You'll see something like `inet 192.168.1.42`. From your laptop on the
same WiFi, open:

```
http://192.168.1.42:8765
```

Your browser prompts for username + password — enter what you set in
step 7. Dashboard loads.

If the page doesn't load:
- Confirm phone and laptop are on the same WiFi network
- Confirm Cero is actually running (`/status` in Telegram, or `ps` in Termux)
- Some routers have "AP isolation" turned on — devices can't reach each
  other. Disable it in the router admin, or fall back to using Telegram +
  SSH for monitoring.

## 12. Daily operation

You don't normally touch the phone. Operations from elsewhere:

| What | How |
|---|---|
| Check status | Telegram: `/status` |
| See latest signals | Telegram: `/readiness` or dashboard readiness card |
| Restart Cero (manual) | SSH into phone, `Ctrl+C` Cero session, restart |
| Update code | Push to GitHub from laptop, on phone `cd ~/cero && git pull && uv sync` |
| Morning validation check | SSH in, `uv run python scripts/morning_check.py` |

To set up SSH:

```bash
# inside Termux on phone:
pkg install openssh
sshd                                 # starts the SSH daemon
passwd                               # set a password
whoami                               # note the username (usually 'u0_axxx')
```

Then from your laptop:

```powershell
ssh -p 8022 u0_axxx@192.168.1.42
```

(Termux uses port 8022, not 22.)

---

## Realistic caveats

### Battery wear

A phone plugged in 24/7 keeps the battery at ~100% charge constantly,
which is the worst state for lithium chemistry. Expect:

- Year 1: noticeable battery degradation
- Year 2: battery may swell (visible as a bulging back panel)
- Year 3+: replace battery, or accept the phone runs only on AC

**Mitigation**: install **Accubattery** from Play Store, set charging
target to 80%. Reduces wear significantly. Or use a smart plug to cycle
power 30 min off / 90 min on (Cero auto-restarts on boot).

### WiFi drops

When WiFi disconnects, ccxt + Telegram retry with backoff. Cero won't
crash. Reconnects automatically once WiFi returns. You may miss a few
signals during the outage.

### Android OS updates

Sometimes break Termux. Disable auto-updates if possible. Major OS
upgrades (Android 13 → 14) are the most risky — read Termux release
notes before accepting.

### Heat

Cero is light on CPU. Should run cool. If your phone heats up running
Cero, you have other things running — quit them.

### Storage growth

Cero's DB grows ~50MB/month. 1GB free storage handles years of
operation. Check with:

```bash
du -sh ~/cero/data/
```

### What to do if Cero stops

Check, in order:
1. Telegram still responsive? If yes, just the dashboard/network is down.
2. Termux still open on phone? If no, battery optimization killed it
   (re-do step 9b).
3. Phone rebooted? If yes, Termux:Boot should have restarted Cero. If it
   didn't, check Settings → Apps → Termux:Boot → Allow auto-start.
4. Logs? In Termux: `tail -50 ~/cero/logs/cero.log`

---

## Reverting to laptop

If you ever want to migrate Cero back to a laptop:

1. Copy `data/cero.db` from phone to laptop (via Termux SSH or USB transfer)
2. Set `web.host: 127.0.0.1` and clear `auth_user`/`auth_pass` in
   `config.yaml`
3. `uv run python -m cero` on the laptop
4. Stop Cero on the phone

All your accumulated validation data comes with you in the DB file.

---

## When to graduate off Android

For real-money mainnet trading with significant capital, an old phone
isn't the right host. Consider when you reach validation gate pass:

- **Oracle Cloud Free** — same $0/month but more reliable (no battery,
  no thermal, redundant power). Setup is harder; needs credit card.
- **A dedicated mini-PC at home** ($100-200 one-time, Intel NUC class) —
  full Linux, persistent, low power.
- **A paid VPS** ($5-10/month, Hetzner / DigitalOcean / Vultr) — most
  reliable per dollar.

Android is the right starting point for the validation period because it
costs $0 and uses hardware you already have. It's not necessarily the
right answer once validation passes and real money is involved.
