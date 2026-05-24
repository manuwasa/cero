"""
Slash command handlers.

Commands to implement:

  /pnl         — performance stats: today, all-time, WR, PF, avg win/loss
  /positions   — currently open positions
  /readiness <SYMBOL>  — score + tier + direction for a symbol
  /economic    — upcoming high-impact events
  /news        — recent headlines
  /trip        — kill switch: cancel orders, close positions, halt
  /reset       — un-trip and resume trading
  /pause       — soft pause: no new entries, manage existing
  /resume      — undo /pause
  /mode <name> — switch between signal_only | approval | auto
  /set <key> <value> — adjust a config value at runtime (saves to config.yaml)
  /status      — overall health: workers OK? exchange connected? equity?

TODO (Claude Code):
- One handler function per command
- All output formatted with monospace blocks for tables
- Confirmation prompts for destructive commands (/trip, /mode auto)
"""
from __future__ import annotations
