"""Is funding-rate harvesting a real edge? Measure it on real Bybit history.

A delta-neutral funding harvest = hold spot LONG + perp SHORT in equal size.
Price direction cancels (you don't predict anything); the short perp COLLECTS the
funding rate that leveraged longs pay (fundingRate > 0 -> shorts get paid). This
is the most plausible retail-accessible, direction-free edge — and unlike
directional trading, it's a HOLD, so per-trade costs amortize to ~nothing.

This pulls real mainnet funding-rate history (public, no keys), sums what a short
perp would have earned, and annualizes it — over the full window AND the recent
45d (to expose regime dependence). Funding can go negative (then you pay), so the
sum of ACTUAL rates is the honest number.

Usage:
    uv run python scripts/funding_analysis.py
    uv run python scripts/funding_analysis.py --days 180
"""
from __future__ import annotations

import argparse

import ccxt

DEFAULT_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT"]


def fetch_funding(ex, symbol, since, now):
    out, cursor = [], since
    while cursor < now:
        try:
            batch = ex.fetch_funding_rate_history(symbol, since=cursor, limit=200)
        except Exception as e:  # noqa: BLE001
            print(f"   ! {symbol} fetch error: {repr(e)[:100]}")
            break
        if not batch:
            break
        out.extend(batch)
        last = batch[-1]["timestamp"]
        if last <= cursor:
            break
        cursor = last + 1
        if len(batch) < 200:
            break
    # dedupe by timestamp
    seen, uniq = set(), []
    for e in out:
        t = e["timestamp"]
        if t in seen:
            continue
        seen.add(t)
        if e.get("fundingRate") is not None:
            uniq.append((t, float(e["fundingRate"])))
    uniq.sort()
    return uniq


def summarize(rows, now):
    """rows = [(ts, rate)]. Returns dict of stats for short-perp (receives +rate)."""
    if not rows:
        return None
    rates = [r for _, r in rows]
    span_days = (rows[-1][0] - rows[0][0]) / 86_400_000 or 1
    gross = sum(rates)                       # fraction of notional over the window
    ann = gross / span_days * 365 * 100      # % annualized
    pos = sum(1 for r in rates if r > 0) / len(rates) * 100
    mean8h = sum(rates) / len(rates) * 100
    # recent 45d
    cut = now - 45 * 86_400_000
    rec = [r for t, r in rows if t >= cut]
    rec_days = 45
    rec_ann = (sum(rec) / rec_days * 365 * 100) if rec else 0.0
    return dict(n=len(rates), days=round(span_days), ann=ann, pos=pos,
                mean8h=mean8h, rec_ann=rec_ann, gross=gross * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    ex = ccxt.bybit({"enableRateLimit": True, "timeout": 30000,
                     "options": {"defaultType": "swap"}})
    now = ex.milliseconds()
    since = now - args.days * 86_400_000

    print(f"\n=== Funding-harvest analysis (Bybit mainnet, ~{args.days}d) ===")
    print("delta-neutral: long spot + short perp; short collects funding when rate>0")
    print("(it's a HOLD, so trading-fee drag amortizes to near-zero)\n")
    print(f"{'symbol':<18}{'intervals':>10}{'%pos':>7}{'mean/8h':>10}{'ann.yield':>11}{'last45d ann':>13}")
    print("-" * 69)
    anns = []
    for sym in symbols:
        rows = fetch_funding(ex, sym, since, now)
        s = summarize(rows, now)
        if not s:
            print(f"{sym:<18}  (no funding data)")
            continue
        anns.append(s["ann"])
        print(f"{sym:<18}{s['n']:>10}{s['pos']:>6.0f}%{s['mean8h']:>+9.4f}%"
              f"{s['ann']:>+10.1f}%{s['rec_ann']:>+12.1f}%")

    if anns:
        avg = sum(anns) / len(anns)
        print(f"\n{'PORTFOLIO avg':<18}{'':<10}{'':>7}{'':>10}{avg:>+10.1f}%")
        print("\ninterpretation:")
        print(f"  ~{avg:+.1f}%/yr gross, delta-neutral (no price-direction bet).")
        print("  net is close to gross for a buy-and-hold hedge (fees amortize), MINUS:")
        print("   - exchange risk (FTX-style), perp-leg liquidation if under-collateralized,")
        print("   - capital on BOTH legs (lower capital efficiency),")
        print("   - funding flips negative in bear phases (see %pos < 100 and any negative cells).")
        print("  Compare to directional TA on majors this session: negative even cost-free.")


if __name__ == "__main__":
    main()
