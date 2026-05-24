"""
Direction inference — long, short, or no trade.

PURE FUNCTION. Takes the criterion results and decides which side (if any) to
trade. Primary signal is criterion 1 (HTF trend); criteria that disagree
veto the trade by collapsing the result to "none".

See docs/CRITERIA.md "Direction logic" for the spec.
"""
from __future__ import annotations

from typing import Literal

from cero.brain.criteria import CriterionResult

Direction = Literal["long", "short", "none"]


def infer_direction(results: list[CriterionResult]) -> Direction:
    """Decide trade direction from a set of criterion results.

    Rules:
      1. Find criterion 1's direction hint (HTF trend). If "flat" / missing → "none".
      2. Any other criterion that fired a direction_hint must agree.
         A conflicting hint (e.g. BOS up while HTF is down) → "none".
      3. Otherwise translate to long/short.
    """
    by_name = {r.name: r for r in results}
    htf = by_name.get("trend_h1_h4")
    if htf is None or not htf.passed or htf.direction_hint is None:
        return "none"

    htf_dir = htf.direction_hint  # "up" or "down"
    for r in results:
        if r.name == "trend_h1_h4":
            continue
        if r.direction_hint is not None and r.direction_hint != htf_dir:
            return "none"

    return "long" if htf_dir == "up" else "short"
