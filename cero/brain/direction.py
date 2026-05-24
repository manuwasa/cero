"""
Direction logic — long, short, or no trade.

PURE FUNCTIONS. Primarily driven by criterion 1 (HTF trend) per docs/CRITERIA.md.

TODO (Claude Code):
- Primary signal: trend_h1_h4 direction_hint
- Override to "none" if HTF trend conflicts with key_levels (don't long into resistance)
- Override to "none" if today's range already > 1.5x H4 ATR (no room left)
"""
from __future__ import annotations
