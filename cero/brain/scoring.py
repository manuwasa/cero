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


# Criteria that must pass for tier A/B. Without these, the strategy's
# "where to enter" check (POI confluence) is missing, so even a high score
# from trend/structure criteria shouldn't graduate the signal past tier C.
#
# This is a hard gate, not a weight. Empirical justification: during the
# Nov 2026 testnet validation, scoring-only tier B emitted ~50 signals/day
# at 7.4% WR because trend/structure criteria pass on nearly every signal
# while poi_alert / ltf_poi rarely fire. Without an entry-precision gate,
# the system was effectively "trade whenever the trend looks right",
# which loses ~78% of one R per trade.
HARD_GATE_CRITERIA: tuple[str, ...] = ("poi_alert",)


def aggregate(results: list[CriterionResult], risk_cfg: RiskConfig) -> ScoreReport:
    """Roll up all 8 results into a ScoreReport."""
    score = sum(r.score for r in results)
    tier = score_to_tier(score, risk_cfg.tier_thresholds)
    direction = infer_direction(results)

    # Hard gate: tier A/B requires every name in HARD_GATE_CRITERIA to have
    # passed. If any didn't, downgrade tier to max C (no trade).
    if tier in ("A", "B"):
        results_by_name = {r.name: r for r in results}
        for required_name in HARD_GATE_CRITERIA:
            r = results_by_name.get(required_name)
            if r is None or not r.passed:
                tier = "C"
                break

    size = risk_cfg.tier_sizing[tier]

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
