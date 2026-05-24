"""
Aggregate criterion results into a tier + direction.

PURE FUNCTIONS. No I/O.

TODO (Claude Code):

def aggregate(results: list[CriterionResult], thresholds: dict[str, int]) -> Scoring:
    score = sum(r.weight for r in results if r.passed)
    tier = score_to_tier(score, thresholds)
    direction = infer_direction(results)
    return Scoring(score=score, tier=tier, direction=direction, results=results)

def score_to_tier(score: int, thresholds: dict[str, int]) -> Literal["A", "B", "C", "D"]: ...
def infer_direction(results: list[CriterionResult]) -> Literal["long", "short", "none"]: ...
"""
from __future__ import annotations
