"""
Aggregate criterion results into a tier + direction + size multiplier.

PURE FUNCTION. The brain calls `aggregate(results, risk_cfg)` and gets back
a `ScoreReport` — everything the executor needs to decide whether to act.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from cero.brain.criteria import CriterionResult
from cero.brain.direction import Direction, infer_direction
from cero.config import RiskConfig

Tier = Literal["A", "B", "C", "D"]


class ScoreReport(BaseModel):
    """Output of the brain for one (symbol, snapshot) — fully serializable."""

    score: int
    tier: Tier
    direction: Direction
    size_multiplier: float       # from risk_cfg.tier_sizing, multiplied with base_risk later
    passed: list[str]            # names of criteria that passed
    failed: list[str]            # names of criteria that failed
    results: list[CriterionResult]

    @property
    def is_actionable(self) -> bool:
        """True when this report should produce a real trade signal:
        A or B tier, with a definite direction, and non-zero sizing."""
        return (
            self.tier in ("A", "B")
            and self.direction in ("long", "short")
            and self.size_multiplier > 0
        )


def score_to_tier(score: int, thresholds: dict[str, int]) -> Tier:
    """Map an integer score to a tier using the boundaries from config.

    thresholds is the validated `risk.tier_thresholds` dict — keys "A", "B", "C"
    define the *minimum* score for that tier; anything below the C threshold
    falls into D.
    """
    if score >= thresholds["A"]:
        return "A"
    if score >= thresholds["B"]:
        return "B"
    if score >= thresholds["C"]:
        return "C"
    return "D"


def aggregate(results: list[CriterionResult], risk_cfg: RiskConfig) -> ScoreReport:
    """Roll up all 8 results into a ScoreReport."""
    score = sum(r.score for r in results)
    tier = score_to_tier(score, risk_cfg.tier_thresholds)
    size = risk_cfg.tier_sizing[tier]
    direction = infer_direction(results)

    # If we can't pick a side, force size to 0 (don't trade) — even if the
    # tier alone would allow it. This is the "HTF flat" / "criteria disagree"
    # safety net described in docs/CRITERIA.md.
    if direction == "none":
        size = 0.0

    return ScoreReport(
        score=score,
        tier=tier,
        direction=direction,
        size_multiplier=size,
        passed=[r.name for r in results if r.passed],
        failed=[r.name for r in results if not r.passed],
        results=results,
    )
