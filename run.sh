#!/usr/bin/env bash
# Cero watchdog — starts the bot and auto-restarts it if it ever exits/crashes.
# Phone/Termux friendly. Uses the project venv if present.
#
# Usage:
#   bash run.sh                                  # foreground (Ctrl+C to stop)
#   nohup bash run.sh > logs/run.out 2>&1 &      # background (./stop.sh to stop)
#   (or run it inside `tmux` so it survives closing the session)
set -u
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python"        # fallback if the venv layout differs

mkdir -p logs
rm -f .cero_stop                   # clear any stale stop flag from a prior run
echo $$ > .cero_watchdog.pid

# Keep Android from sleeping the process (no-op when not on Termux).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

cleanup() {
  rm -f .cero_watchdog.pid
  command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
}
trap cleanup EXIT

echo "cero watchdog up (pid $$) — using $PY. stop with: bash stop.sh"
while [ ! -f .cero_stop ]; do
  echo "$(date '+%F %T') starting cero" | tee -a logs/watchdog.log
  "$PY" -m cero
  code=$?
  [ -f .cero_stop ] && break       # clean stop requested → don't restart
  echo "$(date '+%F %T') cero exited (code $code) — restarting in 5s" | tee -a logs/watchdog.log
  sleep 5
done

rm -f .cero_stop
echo "cero watchdog stopped."
