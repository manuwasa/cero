"""
Calendar worker — pulls upcoming economic events for news-blackout gating.

Uses the FairEconomy JSON feed (which mirrors ForexFactory's calendar):
    https://nfs.faireconomy.media/ff_calendar_thisweek.json

The feed is plain JSON, refreshed daily-ish, and stable enough that we can
parse it without screen-scraping. Each event becomes a `CalendarEvent` row
keyed by `(source, source_id)` — we generate a deterministic id from title +
timestamp + country so repeated polls upsert cleanly.

Brain consumption: `current_blackout(now_ms, news_cfg)` returns
`(is_blackout, event_name)` — used in cero/brain/scheduler.py to pass
`in_blackout` to `build_signal`.

Tests inject a custom `fetcher` callable so we never hit the real network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Awaitable, Callable, Optional

import aiohttp
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cero.brain.risk import in_news_blackout
from cero.config import Config, NewsConfig
from cero.db.models import CalendarEvent
from cero.db.session import session_factory


DEFAULT_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_NAME = "forexfactory"

Fetcher = Callable[[str], Awaitable[str]]


# ──────────────────────────────────────────────────────────────────────
# Worker
# ──────────────────────────────────────────────────────────────────────


class CalendarWorker:
    """Refreshes the `calendar_events` table from the configured feed URL.

    Runs on a much slower cadence than the price/account workers — the calendar
    only changes when events are scheduled or revised. Default: every hour."""

    def __init__(
        self,
        cfg: Config,
        *,
        feed_url: str = DEFAULT_FEED_URL,
        refresh_seconds: int = 3600,
        min_refresh_gap_seconds: int = 1800,   # 30 minutes
        fetcher: Optional[Fetcher] = None,
    ) -> None:
        self.cfg = cfg
        self.feed_url = feed_url
        self.refresh_seconds = refresh_seconds
        self.min_refresh_gap_seconds = min_refresh_gap_seconds
        self.fetcher = fetcher or _http_fetcher
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._log = logger.bind(worker="calendar")

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("CalendarWorker already started")
        self._task = asyncio.create_task(self._loop(), name="calendar_worker")
        self._log.info("started (refresh={}s)", self.refresh_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._log.info("stopped")

    # ── loop ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        # If the DB already has data fetched within `min_refresh_gap_seconds`,
        # skip the initial refresh — this stops rapid Cero restarts from
        # hammering the feed and getting us rate-limited (429s).
        if await self._has_recent_data():
            self._log.info(
                "calendar already fresh — skipping initial fetch, "
                "next refresh in {}s",
                self.refresh_seconds,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_seconds)
            except asyncio.TimeoutError:
                pass

        attempt = 0
        while not self._stop.is_set():
            try:
                await self._refresh()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(900.0, 30.0 * attempt)   # cap at 15 minutes
                self._log.warning(
                    "refresh failed (attempt {}): {} — retry in {}s",
                    attempt, e, delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    # ── refresh ───────────────────────────────────────────────────────

    async def _refresh(self) -> int:
        """Fetch + parse + upsert. Returns the number of events upserted."""
        body = await self.fetcher(self.feed_url)
        events = parse_feed(body)
        if not events:
            self._log.warning("feed parsed to 0 events — feed schema may have changed")
            return 0
        await self._upsert(events)
        self._log.info("refreshed: {} events", len(events))
        return len(events)

    async def _upsert(self, events: list[dict]) -> None:
        async with session_factory()() as s:
            for e in events:
                # SQLite UPSERT via ON CONFLICT against the unique (source,
                # source_id) index. We update everything except the PK.
                stmt = sqlite_insert(CalendarEvent).values(
                    source=SOURCE_NAME,
                    source_id=e["source_id"],
                    ts=e["ts"],
                    name=e["name"],
                    country=e["country"],
                    impact=e["impact"],
                    forecast=e.get("forecast"),
                    previous=e.get("previous"),
                    actual=e.get("actual"),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "ts": stmt.excluded.ts,
                        "name": stmt.excluded.name,
                        "country": stmt.excluded.country,
                        "impact": stmt.excluded.impact,
                        "forecast": stmt.excluded.forecast,
                        "previous": stmt.excluded.previous,
                        "actual": stmt.excluded.actual,
                    },
                )
                await s.execute(stmt)
            await s.commit()

    async def _has_recent_data(self) -> bool:
        """True if any CalendarEvent row was fetched within
        `min_refresh_gap_seconds` of now. Used to skip the initial fetch on
        rapid restarts (avoids 429s from the feed)."""
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.min_refresh_gap_seconds)
        async with session_factory()() as s:
            row = (
                await s.execute(
                    select(CalendarEvent.fetched_at)
                    .where(CalendarEvent.fetched_at >= cutoff)
                    .limit(1)
                )
            ).first()
        return row is not None


# ──────────────────────────────────────────────────────────────────────
# Pure parsing
# ──────────────────────────────────────────────────────────────────────


def parse_feed(body: str) -> list[dict]:
    """Parse the FairEconomy JSON feed into our normalized dicts.

    Each entry has: title, country, date (ISO8601 w/ TZ), impact, optional
    forecast/previous/actual. We coerce impact to lower-case `low|medium|high`
    and build a deterministic source_id so repeated polls upsert."""
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"feed is not JSON: {e}") from e

    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        date = item.get("date")
        country = item.get("country")
        impact = item.get("impact")
        if not (title and date and impact):
            continue
        try:
            ts_ms = _iso_to_ms(date)
        except ValueError:
            continue
        norm_impact = _normalize_impact(impact)
        if norm_impact is None:
            continue
        # Deterministic id so the same event upserts on re-fetch even if its
        # actual/forecast values change.
        sid = hashlib.sha1(f"{title}|{date}|{country}".encode()).hexdigest()[:16]
        out.append({
            "source_id": sid,
            "ts": ts_ms,
            "name": title,
            "country": country or "",
            "impact": norm_impact,
            "forecast": item.get("forecast") or None,
            "previous": item.get("previous") or None,
            "actual": item.get("actual") or None,
        })
    return out


def _iso_to_ms(date: str) -> int:
    """Accept either ISO8601 with tz ('2026-05-02T08:30:00-04:00') or a Z
    suffix ('...Z'). Return unix-ms."""
    if date.endswith("Z"):
        date = date[:-1] + "+00:00"
    dt = datetime.fromisoformat(date)
    return int(dt.timestamp() * 1000)


def _normalize_impact(impact: str) -> Optional[str]:
    s = impact.strip().lower()
    if s in ("high", "red"):
        return "high"
    if s in ("medium", "med", "orange", "moderate"):
        return "medium"
    if s in ("low", "yellow"):
        return "low"
    if s in ("holiday", "non-economic"):
        return None    # holidays aren't tradeable-impact events
    return None


# ──────────────────────────────────────────────────────────────────────
# Blackout query for the brain
# ──────────────────────────────────────────────────────────────────────


async def current_blackout(
    now_ms: int, news_cfg: NewsConfig, *, window_minutes: int = 120
) -> tuple[bool, Optional[str]]:
    """Read events from the DB whose ts is within `window_minutes` of now and
    pass them to `in_news_blackout` from risk.py.

    `window_minutes` doesn't have to match the blackout window — it's just a
    cheap pre-filter so we don't load the whole table when only events near
    `now` matter."""
    window_ms = window_minutes * 60 * 1000
    lo, hi = now_ms - window_ms, now_ms + window_ms
    async with session_factory()() as s:
        rows = (
            await s.execute(
                select(CalendarEvent)
                .where(CalendarEvent.ts.between(lo, hi))
            )
        ).scalars().all()
    return in_news_blackout(list(rows), now_ms, news_cfg)


# ──────────────────────────────────────────────────────────────────────
# Default fetcher
# ──────────────────────────────────────────────────────────────────────


async def _http_fetcher(url: str) -> str:
    """Plain GET. Uses aiohttp's ThreadedResolver same as elsewhere, and a
    user-agent so the feed doesn't 403 us as a bot."""
    headers = {"User-Agent": "Mozilla/5.0 (cero personal-use crypto bot)"}
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.text()
