#!/usr/bin/env bash
# Stop Cero cleanly: signal the watchdog to not restart, then stop the bot.
# Usage: bash stop.sh
set -u
cd "$(dirname "$0")"

touch .cero_stop                            # tell run.sh's loop to exit, not restart
pkill -f "python -m cero" 2>/dev/null       # stop the running bot (triggers clean shutdown)

if [ -f .cero_watchdog.pid ]; then          # stop the watchdog itself if backgrounded
  kill "$(cat .cero_watchdog.pid)" 2>/dev/null
fi

echo "stop signal sent — cero is shutting down."
