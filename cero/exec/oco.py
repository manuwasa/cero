"""
OCO (One-Cancels-Other) logic.

When stop loss fills, cancel take profit (and vice versa). Some exchanges
support native OCO; others need us to watch for fills and cancel manually.

TODO (Claude Code):
- Detect exchange OCO capability via ccxt.has
- If native: use create_order with takeProfitPrice + stopLossPrice
- If not: manually watch for fills and cancel the sibling order
- CRITICAL: this code path MUST be bulletproof. A failed cancel leaves
  a naked stop in the book that can fill unexpectedly later.
"""
from __future__ import annotations
