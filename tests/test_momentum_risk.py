"""Tests for the momentum risk overlay: inverse-vol weighting, volatility
targeting, the circuit breaker, and the book's flatten-on-halt behavior.

These are pure-function tests (plus one temp-DB book test) — no network."""
from __future__ import annotations

import os
import tempfile

import pytest

from cero.brain.momentum import (
    MomentumBook,
    MomentumConfig,
    circuit_breach,
    portfolio_vol,
    realized_vol,
    target_weights,
)


def make_closes(n_syms: int = 8, n: int = 70) -> dict[str, list[float]]:
    """Synthetic daily closes. Drift increases with i (so momentum ranks
    S0<...<S7), with an alternating wiggle whose amplitude sets each coin's
    volatility. Lookbacks are even and the series length odd, so the wiggle
    cancels in the L-day momentum return — ranking stays purely drift-driven."""
    amp = {6: 0.005, 7: 0.05}     # S7 = high vol, S6 = low vol (both end up long)
    out: dict[str, list[float]] = {}
    for i in range(n_syms):
        g, a = 0.001 * i, amp.get(i, 0.01)
        out[f"S{i}"] = [(1 + g) ** t * (1 + a * ((-1) ** t)) for t in range(n)]
    return out


def base_cfg(**kw) -> MomentumConfig:
    defaults = dict(
        universe=tuple(f"S{i}" for i in range(8)),
        lookbacks=(20, 30, 60), frac=0.30, rebalance_days=5,
        gross_per_side=1.0, weighting="inverse_vol", vol_window=30,
        target_vol=0.0, max_gross_per_side=1.0,
        daily_loss_halt_pct=0.0, drawdown_halt_pct=0.0,
    )
    defaults.update(kw)
    return MomentumConfig(**defaults)


# ── realized_vol ──────────────────────────────────────────────────────

def test_realized_vol_constant_is_zero():
    assert realized_vol([100.0] * 40, window=30) == 0.0


def test_realized_vol_rises_with_amplitude():
    closes = make_closes()
    assert realized_vol(closes["S7"], 30) > realized_vol(closes["S6"], 30)


def test_realized_vol_insufficient_history():
    assert realized_vol([100.0], window=30) == 0.0


# ── target_weights: neutrality + inverse-vol sizing ───────────────────

def test_weights_dollar_neutral():
    w = target_weights(make_closes(), base_cfg())
    assert w, "expected a non-empty book"
    longs = sum(x for x in w.values() if x > 0)
    shorts = sum(x for x in w.values() if x < 0)
    assert longs == pytest.approx(1.0, abs=1e-9)     # leg sums to gross_per_side
    assert shorts == pytest.approx(-1.0, abs=1e-9)
    assert sum(w.values()) == pytest.approx(0.0, abs=1e-9)   # net zero


def test_inverse_vol_shrinks_the_wild_coin():
    w = target_weights(make_closes(), base_cfg())
    # S6 and S7 are the two longs; S7 is far more volatile → smaller position
    assert w["S7"] > 0 and w["S6"] > 0
    assert abs(w["S7"]) < abs(w["S6"])


def test_equal_weighting_ignores_vol():
    w = target_weights(make_closes(), base_cfg(weighting="equal"))
    assert abs(w["S7"]) == pytest.approx(abs(w["S6"]), abs=1e-9)


# ── volatility targeting + leverage cap ───────────────────────────────

def test_vol_target_delevers_when_book_is_hot():
    closes = make_closes()
    # measure the un-targeted book's vol, then target HALF of it → ~half gross
    unscaled = target_weights(closes, base_cfg(target_vol=0.0))
    pv = portfolio_vol(unscaled, closes, 30)
    assert pv > 0
    delevered = target_weights(closes, base_cfg(target_vol=pv / 2))
    gross = sum(abs(x) for x in delevered.values() if x > 0)
    assert gross == pytest.approx(0.5, rel=0.05)


def test_vol_target_never_exceeds_cap():
    closes = make_closes()
    # huge target would lever up, but max_gross_per_side caps it at the base
    w = target_weights(closes, base_cfg(target_vol=5.0, max_gross_per_side=1.0))
    gross = sum(abs(x) for x in w.values() if x > 0)
    assert gross == pytest.approx(1.0, abs=1e-6)


# ── circuit breaker ───────────────────────────────────────────────────

def test_drawdown_breach():
    cfg = base_cfg(drawdown_halt_pct=15.0)
    assert circuit_breach(84.0, 100.0, 0.0, cfg) is not None       # -16% from peak
    assert circuit_breach(90.0, 100.0, 0.0, cfg) is None           # -10% is fine


def test_daily_loss_breach():
    cfg = base_cfg(daily_loss_halt_pct=8.0)
    assert circuit_breach(92.0, 100.0, -8.0, cfg) is not None      # -8% this cycle
    assert circuit_breach(95.0, 100.0, -5.0, cfg) is None          # -5% is fine


def test_halts_disabled_when_zero():
    cfg = base_cfg(daily_loss_halt_pct=0.0, drawdown_halt_pct=0.0)
    assert circuit_breach(1.0, 100.0, -99.0, cfg) is None


# ── MomentumBook: flatten on halt ─────────────────────────────────────

def _fresh_book(**cfg_kw):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cfg = base_cfg(rebalance_days=0, weighting="equal", target_vol=0.0, **cfg_kw)
    book = MomentumBook(cfg, db_path=path, start_equity=10_000.0)
    closes = make_closes()
    book.update(closes, now_ms=0)            # first cycle rebalances → positions on
    return book, closes, path


def test_external_halt_flattens_the_book():
    book, closes, path = _fresh_book()
    try:
        before = book.update(closes, now_ms=10, do_rebalance=False)
        assert before["longs"] or before["shorts"], "book should be holding"
        s = book.update(closes, now_ms=20, do_rebalance=False, external_halt=True)
        assert s["flattened"] is True
        assert s["halt_reason"] == "kill switch (/trip)"
        assert not s["longs"] and not s["shorts"]
    finally:
        os.remove(path)


def test_drawdown_breach_flattens_the_book():
    book, closes, path = _fresh_book(drawdown_halt_pct=10.0)
    try:
        # crash every held position adversely: longs down 25%, shorts up 25%
        held = book.update(closes, now_ms=10, do_rebalance=False)
        crashed = {s: list(c) for s, c in closes.items()}
        for sym in held["longs"]:
            crashed[sym][-1] *= 0.75
        for sym in held["shorts"]:
            crashed[sym][-1] *= 1.25
        s = book.update(crashed, now_ms=20, do_rebalance=False)
        assert s["halt_reason"] is not None and "drawdown" in s["halt_reason"]
        assert s["flattened"] is True
        assert not s["longs"] and not s["shorts"]
    finally:
        os.remove(path)
