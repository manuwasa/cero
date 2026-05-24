"""
Account worker.

Keeps account balance, equity, open positions, and fills in sync with the
exchange. Both via WebSocket (push) and periodic REST (reconciliation).

TODO (Claude Code):
- watch_balance + watch_orders + watch_positions
- Periodic REST sweep every 30s as a sanity check
- Detect unexpected positions (manual trades elsewhere) and TRIP if found
"""
from __future__ import annotations
