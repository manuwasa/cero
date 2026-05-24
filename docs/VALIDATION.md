# Validation

When are you allowed to trust your strategy and put more money on it? This document is the answer. **Read it before you scale up.**

## The gate

Before flipping `auto_trade: true` or scaling capital:

1. **Sample size:** at least **200 trades**
2. **Win rate:** sustained **≥ 55%**
3. **Profit factor:** **≥ 1.5** (cumulative wins / cumulative losses)
4. **Stability:** win rate of first 100 trades within 5% of last 100 trades (no degradation)
5. **Max drawdown:** under your tolerance (e.g. < 20%)

All five must pass. If any fail, you do not scale. You research.

## Why 200 trades

Trading is statistics. Small samples lie.

For a true 55% win rate strategy:

| Sample size | Observed WR could realistically be (95% CI) |
| --- | --- |
| 10  | 25% — 85% |
| 30  | 38% — 73% |
| 50  | 41% — 69% |
| 100 | 45% — 65% |
| **200** | **48% — 62%** |
| 500 | 50% — 60% |
| 1000 | 52% — 58% |

At 30 trades, your "56% win rate strategy" might really be a 40% loser you got lucky on. At 200, the confidence band narrows enough that you can actually distinguish edge from luck.

Why not require 500 or 1000? Because:
- It takes too long to collect at retail trading frequency
- 200 is the practical compromise — significant enough to act on, fast enough to gather
- You'll keep watching after 200 anyway; the gate is "minimum," not "celebrate and stop tracking"

## Why 55% specifically

Three reasons:

**1. Coin-flip floor.** A true 50% win rate strategy with equal win/loss sizes makes nothing minus fees. You need to clear that with margin.

**2. Degradation buffer.** Live performance is almost always worse than backtest. A 55% in-sample edge often shows 50-52% live. If your live target is 55%, your backtest needs to show ~60%.

**3. Robustness check.** A strategy that only "works" at exactly 55% is fragile. One slightly bad week and you're at 52% and panicking. Wanting margin above the floor is psychological as much as statistical.

## Why profit factor matters more than win rate alone

Profit factor (PF) = total wins / total losses.

A strategy can win 70% of the time and lose money:
- 70 wins × $1 = $70
- 30 losses × $3 = $90
- Net: **−$20**, PF = 0.78 → loser

A strategy can win 40% of the time and crush:
- 40 wins × $3 = $120
- 60 losses × $1 = $60
- Net: **+$60**, PF = 2.0 → winner

Always check both. The screenshot example shows WR 56% with PF 2.88 — that's an excellent combination. PF ≥ 1.5 is healthy; PF ≥ 2.0 is strong; PF ≥ 3.0 is exceptional (and probably overfit, scrutinize hard).

## Why stability matters

A strategy that worked for 100 trades and stopped working for the next 100 is **decaying**. Causes:

- Market regime changed (trending → ranging, or vice versa)
- Your edge has been arbed away
- You unconsciously changed how you apply rules (in signal_only mode)
- The original 100 was lucky variance

Check: split your 200 trades into first 100 and last 100. Compare:
- Win rate difference > 5%? Investigate.
- PF difference > 0.5? Investigate.
- Drawdown clustered in second half? Investigate.

If stable → green light. If degrading → diagnose before scaling.

## Why max drawdown caps you

Max drawdown (MDD) = largest peak-to-trough loss in your equity curve.

You can have a profitable strategy with 60% drawdown — and you'll quit using it before it recovers, because watching 60% of your account evaporate is psychologically devastating regardless of math.

Set your own MDD tolerance honestly. Most retail can handle 15-25% before they panic-override the bot. Strategies with MDD over your tolerance need:
- Smaller position sizes (lower base risk %)
- Fewer concurrent positions
- Tighter stops
- A volatility filter (don't trade during high-vol regimes)

## Scaling rules

After passing the gate, scale **gradually**:

```
Stage     Capital      What you're testing
─────     ───────      ───────────────────
1         $100         Strategy works at all
2         $500         Strategy works after slight slippage
3         $2,000       Strategy works through real market hours
4         $10,000      Strategy works when losses sting
5         $50,000      Strategy works with execution-quality concerns
6         $200,000+    Now you have an actual edge
```

**Each stage:** re-validate. Run 100-200 trades at the new size. Check all five gates again. Then scale.

**Why each stage matters:**

- **$100 → $500:** Trade size still tiny vs market. Mostly tests "does the bot work."
- **$500 → $2,000:** Your trades start being noticed by the order book. Slippage appears. Spreads matter.
- **$2,000 → $10,000:** Losses start being psychologically meaningful. You'll be tempted to override.
- **$10,000 → $50,000:** Multi-thousand-dollar moves on single trades. Real risk of overriding the system.
- **$50,000+:** Now liquidity matters. You may need to split orders, time entries to avoid price impact.

Skipping stages is how accounts die.

## How to track this

In `cero.db`, the `trades` table stores everything you need. Compute these stats anytime via `/pnl` in Telegram or the dashboard:

```
PNL
─────────────────────────────────
trades:           247
win rate:         57.1% (141/247)
profit factor:    2.31
avg win:          +0.42
avg loss:         -0.18
max drawdown:     -8.3% (from 24 trades ago)
first 100 WR:     56.0%
last 100 WR:      58.0%   ← stable
stage:            2 ($500)
gate status:      PASS ✓
```

The gate status should be computed automatically. When it's a PASS, the dashboard shows a green "Eligible to scale to Stage 3" banner. **The decision is still manual.** Cero recommends; you decide.

## What NOT to do

- ❌ Scale after a winning week. Variance.
- ❌ Scale because the chart "looks easy right now." Markets change.
- ❌ Skip stages because you're bored. The dead bodies in trading were all impatient.
- ❌ Lower the gate thresholds when you fail them. You're cheating yourself.
- ❌ Restart the count after a losing streak to "give the strategy a clean slate." That's data hiding from yourself.
- ❌ Compare your bot to other people's results. Different markets, different rules, different luck.

## What TO do when you fail the gate

- Diagnose **specifically** why. Which criterion's win rate is below expectations? Which symbol is dragging the average? Which time of day is killing you?
- Look at losing trades individually. Pattern? Often it's one type of setup that doesn't work in current regime.
- Adjust **one thing**, validate again. Not five things. One.
- If the strategy fundamentally doesn't work: kill it. The discipline to abandon a broken strategy is harder and more valuable than the discipline to validate one.

## The deeper principle

The 200-trade gate isn't a magic number. The principle is:

> **Pre-commit to your validation criteria when you're calm and rational. Hold yourself to them when you're excited and emotional.**

That's what separates traders who survive from traders who don't. Your future self — the one watching a 7-trade win streak and feeling invincible — will try to negotiate down the gate. Your present self, writing this, gets to say no.

This document is you saying no to yourself in advance. Respect it.
