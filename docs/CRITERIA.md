# The 8 Criteria

Cero scores every potential setup against 8 criteria. Each criterion is a **pure function** that takes market data and returns a pass/fail + score. The total determines the tier.

> **This document defines the strategy.** Changes here are strategy changes. Discuss before editing.

## How scoring works

Each criterion has a weight. Weights sum to **100**. A criterion either fully passes (full weight added) or fails (0 added). Total score → tier:

| Score | Tier | Sizing |
| --- | --- | --- |
| 80-100 | **A** | 1.0× base risk |
| 60-79  | **B** | 0.5× base risk (reduced) |
| 40-59  | **C** | 0 (no trade) |
| 0-39   | **D** | 0 (no trade) |

Tier thresholds are in `config.yaml` — adjustable.

---

## The criteria

### 1. Trend confirmed (H1 & H4) — weight 20

**Question:** Is the bigger-timeframe trend clear and aligned?

**Logic:**
- Compute 50-EMA on 1H candles
- Compute 50-EMA on 4H candles
- 1H trend = `up` if price > EMA AND EMA slope > 0, `down` if opposite, else `flat`
- Same for 4H
- **Pass if:** both 1H and 4H show same non-flat trend

**Direction:** sets the trade direction (long if both up, short if both down)

**Why it matters:** Trading against HTF trend is a losing game over time. This is the foundation.

---

### 2. Clean market structure — weight 18

**Question:** Is the price action structured (clear swings) or choppy?

**Logic:**
- On 1H, identify last 5 swing highs and swing lows (fractal: high with N lower highs on each side)
- A trend has clear higher highs + higher lows (or lower highs + lower lows)
- The most recent move should be a **Break of Structure (BOS)** in the direction of the trend
- **Pass if:** latest BOS aligns with HTF trend direction from criterion 1

**Why it matters:** Choppy markets eat stop losses. Clear structure means moves follow through.

---

### 3. Key levels marked — weight 10

**Question:** Are there well-defined support/resistance or round numbers near current price?

**Logic:**
- Identify horizontal levels: prior 1H/4H swing highs/lows, daily highs/lows, weekly highs/lows
- Identify round numbers: in BTC, every $1,000; in ETH, every $100; etc.
- Cluster levels within 0.2% of each other into "zones"
- **Pass if:** at least one support zone exists below price (for longs) or resistance above (for shorts), AND at least one within 1× ATR(14, H1) of current price

**Why it matters:** Trades need defined reference points. Targets and stops live at these levels.

---

### 4. Alert on POI (OTE zone) — weight 15

**Question:** Is price entering an Optimal Trade Entry zone or unmitigated Point of Interest?

**Logic:**
- Identify the most recent BOS from criterion 2
- Compute the leg that caused it (low to high for bullish BOS, high to low for bearish)
- OTE zone = Fib 62-79% retracement of that leg
- Also identify any unmitigated FVG (Fair Value Gap — 3-candle imbalance) or order block within the trend direction
- **Pass if:** current price is within an OTE zone OR within an unmitigated FVG/order block aligned with HTF direction

**Why it matters:** This is *where* to enter. Entering anywhere is a coin flip; entering at a POI is an edge.

---

### 5. Session high/low marked — weight 5

**Question:** Has today's session high and low been identified, and is price respecting them?

**Logic:**
- Compute today's session high (`session_h`) and low (`session_l`)
- **Pass if:** both are identifiable AND price has reacted to one of them recently (last 4 hours touched and reversed, OR cleanly broke and retested)

**Why it matters:** Session extremes are battle lines. Trades that respect them tend to follow through.

---

### 6. Structure 15m / 30m aligned — weight 12

**Question:** Does the LTF (lower timeframe) structure confirm the HTF setup?

**Logic:**
- Apply criterion 2's logic on 15m and 30m candles
- Get LTF trend direction
- **Pass if:** LTF trend agrees with HTF direction from criterion 1, OR shows a fresh BOS in HTF direction (catching a reversal at the POI)

**Why it matters:** HTF says "where to look." LTF says "when to pull the trigger."

---

### 7. POI identified on LTF — weight 12

**Question:** Is there a Lower-Timeframe Point of Interest near current price?

**Logic:**
- Apply criterion 4's logic on 5m / 15m candles
- Find OTE zones, FVGs, order blocks
- **Pass if:** an LTF POI exists within 0.3× ATR(14, H1) of current price

**Why it matters:** Refines the entry. HTF POI gives the zone; LTF POI gives the price.

---

### 8. Price move < H4 ATR — weight 8

**Question:** Has price already moved too much today (no room left)?

**Logic:**
- Compute today's range: `daily_high - daily_low`
- Compute 4H ATR(14)
- **Pass if:** today's range < H4 ATR (room left for another leg)

**Why it matters:** If price has already moved 2× ATR today, chasing now means buying high / shorting low. Wait.

---

## Direction logic

Direction is decided primarily by criterion 1 (HTF trend):

- Both H1 and H4 up → consider longs only
- Both H1 and H4 down → consider shorts only
- Conflicting or flat → no trade regardless of score

Criterion 4 (POI) gives the entry zone. Combined: "we go long at the bullish POI when both H1 and H4 are bullish AND price has retraced into the OTE."

---

## Why this set?

These 8 criteria represent a **trend-following + retracement-entry** style, common in SMC/ICT trading. The structure:

- **Context (1, 2, 3, 5)** — what's the bigger picture saying?
- **Entry (4, 6, 7)** — where exactly to enter?
- **Sanity (8)** — is now the right time?

You can swap criteria as you learn. Just keep weights summing to 100 and document why.

---

## What's intentionally NOT in here

- **Volume** — crypto volume data is noisy (wash trading, exchange-specific quirks). Adding it would need careful normalization.
- **Sentiment / news scoring** — handled separately as a *blackout* (don't trade near news), not as a *signal*.
- **Indicators (RSI, MACD, etc.)** — would add noise. Structure-based criteria already capture trend and momentum.
- **Multi-symbol correlation** — adds complexity without proven edge for single-symbol trading.

If you want any of these, propose, test in isolation, then add — with a weight that doesn't disrupt the current balance.

---

## Tier examples (for intuition)

| Scenario | Criteria passing | Score | Tier |
| --- | --- | --- | --- |
| Perfect alignment | 1+2+3+4+5+6+7+8 | 100 | A — full size |
| Missing LTF POI | 1+2+3+4+5+6+8 | 88 | A — full size |
| LTF disagrees | 1+2+3+4+5+7+8 | 80 | A — barely |
| No HTF trend | 2+3+5+6+7+8 | 60 | B — half size |
| Choppy market | 1+3+5+8 | 43 | C — no trade |
| Almost nothing | 5+8 | 13 | D — no trade |

The screenshots from the inspiration project show real-world examples: HYPE scored 5/100 (D, no trade), BTC scored 53/100 (B, half size with the "reduced risk" banner). These tier outcomes match what this scoring system would produce.

---

## Implementation note

Every criterion in `cero/brain/criteria.py` should look like:

```python
def trend_h1_h4(ctx: MarketContext) -> CriterionResult:
    """Criterion 1: HTF trend confirmed and aligned."""
    h1_trend = compute_trend(ctx.candles_1h)
    h4_trend = compute_trend(ctx.candles_4h)
    
    passed = (
        h1_trend in ("up", "down") 
        and h1_trend == h4_trend
    )
    
    return CriterionResult(
        name="trend_h1_h4",
        weight=20,
        passed=passed,
        detail=f"H1={h1_trend}, H4={h4_trend}",
        direction_hint=h1_trend if passed else None,
    )
```

Pure function. Takes context, returns result. No I/O. Easy to test.
