"""Tests for direction inference + score aggregation."""
from __future__ import annotations

import pytest

from cero.brain.criteria import CriterionResult
from cero.brain.direction import infer_direction
from cero.brain.scoring import aggregate, score_to_tier
from cero.config import RiskConfig

RISK = RiskConfig(
    base_risk_per_trade_pct=0.5,
    max_daily_loss_pct=3.0,
    max_consecutive_losses=4,
    max_concurrent_positions=3,
    tier_sizing={"A": 1.0, "B": 0.5, "C": 0.0, "D": 0.0},
    tier_thresholds={"A": 80, "B": 60, "C": 40},
)

C1_NAMES = (
    "trend_h1_h4", "market_structure", "key_levels", "poi_alert",
    "session_hl", "structure_15m_30m", "ltf_poi", "atr_room",
)
WEIGHTS = (20, 18, 10, 15, 5, 12, 12, 8)


def _result(name: str, weight: int, *, passed: bool, direction_hint=None) -> CriterionResult:
    return CriterionResult(
        name=name, weight=weight, passed=passed,
        detail="test", direction_hint=direction_hint,
    )


def _results_all(pass_mask: list[bool], hints: dict[str, str] | None = None) -> list[CriterionResult]:
    hints = hints or {}
    return [
        _result(n, w, passed=p, direction_hint=hints.get(n))
        for n, w, p in zip(C1_NAMES, WEIGHTS, pass_mask)
    ]


# ──────────────────────────────────────────────────────────────────────
# score_to_tier
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("score,expected", [
    (100, "A"),
    (80, "A"),
    (79, "B"),
    (60, "B"),
    (59, "C"),
    (40, "C"),
    (39, "D"),
    (0, "D"),
])
def test_score_to_tier_boundaries(score: int, expected: str):
    assert score_to_tier(score, RISK.tier_thresholds) == expected


# ──────────────────────────────────────────────────────────────────────
# infer_direction
# ──────────────────────────────────────────────────────────────────────


def test_direction_long_when_htf_up_and_no_conflicts():
    results = _results_all(
        pass_mask=[True] * 8,
        hints={"trend_h1_h4": "up", "market_structure": "up", "poi_alert": "up"},
    )
    assert infer_direction(results) == "long"


def test_direction_short_when_htf_down():
    results = _results_all(
        pass_mask=[True] * 8,
        hints={"trend_h1_h4": "down"},
    )
    assert infer_direction(results) == "short"


def test_direction_none_when_htf_did_not_pass():
    results = _results_all(
        pass_mask=[False] + [True] * 7,  # c1 failed
        hints={"market_structure": "up"},
    )
    assert infer_direction(results) == "none"


def test_direction_none_on_conflicting_hint():
    # HTF says up but BOS says down → no trade
    results = _results_all(
        pass_mask=[True] * 8,
        hints={"trend_h1_h4": "up", "market_structure": "down"},
    )
    assert infer_direction(results) == "none"


# ──────────────────────────────────────────────────────────────────────
# aggregate
# ──────────────────────────────────────────────────────────────────────


def test_aggregate_all_pass_yields_tier_a_long_full_size():
    results = _results_all(
        pass_mask=[True] * 8,
        hints={"trend_h1_h4": "up"},
    )
    rep = aggregate(results, RISK)
    assert rep.score == 100
    assert rep.tier == "A"
    assert rep.direction == "long"
    assert rep.size_multiplier == 1.0
    assert rep.is_actionable is True


def test_aggregate_tier_b_half_size():
    # Drop c2 (18) + c4 (15) → 100 - 33 = 67 → B
    results = _results_all(
        pass_mask=[True, False, True, False, True, True, True, True],
        hints={"trend_h1_h4": "up"},
    )
    rep = aggregate(results, RISK)
    assert rep.score == 67
    assert rep.tier == "B"
    assert rep.size_multiplier == 0.5
    assert rep.direction == "long"
    assert rep.is_actionable is True


def test_aggregate_tier_c_no_trade():
    # Pass only weights summing 8+5+12+12+10 = 47 → C
    results = _results_all(
        pass_mask=[False, False, True, False, True, True, True, True],
        hints={},
    )
    rep = aggregate(results, RISK)
    assert rep.score == 47
    assert rep.tier == "C"
    assert rep.size_multiplier == 0.0
    assert rep.is_actionable is False


def test_aggregate_none_direction_forces_size_zero_even_if_tier_a():
    # All passed (score 100 → A) but conflicting hints → direction "none"
    results = _results_all(
        pass_mask=[True] * 8,
        hints={"trend_h1_h4": "up", "market_structure": "down"},
    )
    rep = aggregate(results, RISK)
    assert rep.tier == "A"
    assert rep.direction == "none"
    assert rep.size_multiplier == 0.0
    assert rep.is_actionable is False


def test_aggregate_records_passed_and_failed_names():
    results = _results_all(
        pass_mask=[True, False, True, True, False, True, True, False],
        hints={"trend_h1_h4": "up"},
    )
    rep = aggregate(results, RISK)
    assert set(rep.passed) == {"trend_h1_h4", "key_levels", "poi_alert", "structure_15m_30m", "ltf_poi"}
    assert set(rep.failed) == {"market_structure", "session_hl", "atr_room"}
