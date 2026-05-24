"""Tests for cero/data/news_worker.py."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from cero.config import (
    AlertsConfig, Config, CriteriaWeights, DatabaseConfig, ExchangeConfig,
    LoggingConfig, NewsConfig, RiskConfig, WebConfig,
)
from cero.data.news_worker import NewsWorker, parse_feed, _short_source
from cero.db.models import NewsItem
from cero.db.session import close_db, init_db, session_factory


# ──────────────────────────────────────────────────────────────────────
# Fixtures + sample feeds
# ──────────────────────────────────────────────────────────────────────


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample Feed</title>
    <item>
      <title>Bitcoin hits new high</title>
      <link>https://example.com/btc-high</link>
      <description>BTC up 5%</description>
      <pubDate>Mon, 24 May 2026 14:00:00 GMT</pubDate>
      <guid isPermaLink="false">btc-001</guid>
      <author>alice@example.com</author>
    </item>
    <item>
      <title>ETH rallies on upgrade</title>
      <link>https://example.com/eth-rally</link>
      <pubDate>Mon, 24 May 2026 13:30:00 GMT</pubDate>
      <guid>eth-002</guid>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Sample</title>
  <entry>
    <id>tag:example.com,2026:abc-1</id>
    <title>Solana network outage</title>
    <link href="https://example.com/sol-out"/>
    <published>2026-05-24T12:00:00Z</published>
    <summary>Network down 30 min</summary>
  </entry>
</feed>
"""

NO_DATE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
  <title>Undated item</title>
  <link>https://example.com/x</link>
  <guid>x</guid>
</item></channel></rss>
"""


def _cfg(db_path: Path, feeds=None) -> Config:
    if feeds is None:
        feeds = ["https://example.com/rss"]
    return Config(
        exchange=ExchangeConfig(name="bybit", testnet=True),
        symbols=["ETH/USDT:USDT"], timeframes=["5m", "1h"],
        backfill_candles=300, account_poll_seconds=10, mode="signal_only",
        risk=RiskConfig(
            base_risk_per_trade_pct=0.5, max_daily_loss_pct=3.0,
            max_consecutive_losses=4, max_concurrent_positions=3,
            tier_sizing={"A": 1.0, "B": 0.5, "C": 0.0, "D": 0.0},
            tier_thresholds={"A": 80, "B": 60, "C": 40},
        ),
        criteria_weights=CriteriaWeights(
            trend_h1_h4=20, market_structure=18, key_levels=10, poi_alert=15,
            session_hl=5, structure_15m_30m=12, ltf_poi=12, atr_room=8,
        ),
        news=NewsConfig(
            blackout_minutes_before=15, blackout_minutes_after=15,
            blackout_impacts=["high"], sources=[],
            rss_feeds=feeds,
            twitter_watchlist=[],
        ),
        alerts=AlertsConfig(), web=WebConfig(),
        database=DatabaseConfig(path=str(db_path), echo=False),
        logging=LoggingConfig(),
    )


@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_news.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────
# parse_feed
# ──────────────────────────────────────────────────────────────────────


def test_parse_rss_extracts_items():
    items = parse_feed(SAMPLE_RSS, source_url="https://example.com/rss")
    assert len(items) == 2
    assert items[0]["content"] == "Bitcoin hits new high"
    assert items[0]["source"] == "example.com"
    assert items[0]["source_id"] == "btc-001"
    assert items[0]["url"] == "https://example.com/btc-high"
    assert items[0]["ts"] > 0


def test_parse_atom_extracts_items():
    items = parse_feed(SAMPLE_ATOM, source_url="https://example.com/atom")
    assert len(items) == 1
    assert items[0]["content"] == "Solana network outage"
    assert items[0]["url"] == "https://example.com/sol-out"
    assert items[0]["source_id"].startswith("tag:example.com")


def test_parse_skips_undated_items():
    items = parse_feed(NO_DATE_RSS, source_url="https://example.com/rss")
    assert items == []


def test_parse_rejects_invalid_xml():
    with pytest.raises(ValueError):
        parse_feed("<not-valid", source_url="x")


def test_short_source_uses_hostname():
    assert _short_source("https://www.cointelegraph.com/rss") == "cointelegraph.com"
    assert _short_source("https://feeds.reddit.com/r/x") == "feeds.reddit.com"
    assert _short_source("") == "unknown"


def test_parse_strips_html_from_titles():
    feed = """<?xml version="1.0"?><rss><channel><item>
      <title>&lt;b&gt;Big&lt;/b&gt; news</title>
      <link>https://example.com/x</link>
      <pubDate>Mon, 24 May 2026 14:00:00 GMT</pubDate>
      <guid>x</guid>
    </item></channel></rss>"""
    items = parse_feed(feed, source_url="https://example.com/rss")
    assert items[0]["content"] == "Big news"


# ──────────────────────────────────────────────────────────────────────
# Worker refresh + upsert
# ──────────────────────────────────────────────────────────────────────


async def test_refresh_writes_items(temp_db):
    cfg = _cfg(temp_db)

    async def fetcher(_url: str) -> str:
        return SAMPLE_RSS

    w = NewsWorker(cfg, fetcher=fetcher)
    n = await w._refresh_all()
    assert n == 2

    async with session_factory()() as s:
        rows = (await s.execute(select(NewsItem))).scalars().all()
    assert len(rows) == 2
    assert all(r.source == "example.com" for r in rows)


async def test_refresh_upserts_on_repeat(temp_db):
    cfg = _cfg(temp_db)

    async def fetcher(_url: str) -> str:
        return SAMPLE_RSS

    w = NewsWorker(cfg, fetcher=fetcher)
    await w._refresh_all()
    await w._refresh_all()    # second pass — should not duplicate

    async with session_factory()() as s:
        rows = (await s.execute(select(NewsItem))).scalars().all()
    assert len(rows) == 2


async def test_one_failing_feed_doesnt_stop_others(temp_db):
    cfg = _cfg(temp_db, feeds=[
        "https://a.example.com/feed",
        "https://b.example.com/feed",
    ])
    calls = {"count": 0}

    async def fetcher(url: str) -> str:
        if "a.example" in url:
            raise RuntimeError("connection refused")
        calls["count"] += 1
        return SAMPLE_RSS

    w = NewsWorker(cfg, fetcher=fetcher)
    n = await w._refresh_all()
    # Only b returned items; a failed silently.
    assert n == 2
    assert calls["count"] == 1
    async with session_factory()() as s:
        rows = (await s.execute(select(NewsItem))).scalars().all()
    assert {r.source for r in rows} == {"b.example.com"}


async def test_no_rss_feeds_means_worker_idle(temp_db):
    cfg = _cfg(temp_db, feeds=[])
    w = NewsWorker(cfg, fetcher=lambda u: (_ for _ in ()).throw(Exception()))  # type: ignore[arg-type]
    w.start()
    # Should not have created a task
    assert w._task is None
    await w.stop()
