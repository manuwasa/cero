"""
News worker — pulls crypto headlines from RSS feeds.

Reads `cfg.news.rss_feeds` (list of URLs) and writes each item to the `news`
table. Refresh cadence is slow (15 min by default) — news cycles aren't
second-fast and most feeds throttle aggressive polling.

**This worker never gates trading.** Headlines are context only; scheduled
news blackouts come from calendar_worker. If RSS scraping fails entirely the
rest of the system keeps running.

Parsing is minimal-dependency: stdlib `xml.etree.ElementTree` handles both
RSS 2.0 and Atom feed shapes. Tests inject a custom fetcher so we never hit
the real network.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import aiohttp
from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cero.config import Config
from cero.db.models import NewsItem
from cero.db.session import session_factory


Fetcher = Callable[[str], Awaitable[str]]

# Atom uses xmlns prefixes; we strip them in the parser so both RSS + Atom
# fall through the same code path.
_NS_RE = re.compile(r"^\{[^}]+\}")


# ──────────────────────────────────────────────────────────────────────
# Worker
# ──────────────────────────────────────────────────────────────────────


class NewsWorker:
    """Polls every configured RSS feed periodically."""

    def __init__(
        self,
        cfg: Config,
        *,
        refresh_seconds: int = 900,    # 15 minutes
        fetcher: Optional[Fetcher] = None,
    ) -> None:
        self.cfg = cfg
        self.refresh_seconds = refresh_seconds
        self.fetcher = fetcher or _http_fetcher
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._log = logger.bind(worker="news")

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("NewsWorker already started")
        if not self.cfg.news.rss_feeds:
            self._log.warning("no RSS feeds configured — worker idle")
            return
        self._task = asyncio.create_task(self._loop(), name="news_worker")
        self._log.info(
            "started ({} feed{}, refresh={}s)",
            len(self.cfg.news.rss_feeds),
            "s" if len(self.cfg.news.rss_feeds) != 1 else "",
            self.refresh_seconds,
        )

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
        # First refresh runs immediately on start (so the dashboard isn't empty
        # for 15 minutes), then settles into the regular cadence.
        while not self._stop.is_set():
            await self._refresh_all()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.refresh_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _refresh_all(self) -> int:
        """One pass over every configured feed. Each feed is independent —
        one failure doesn't affect the others."""
        total = 0
        for url in self.cfg.news.rss_feeds:
            try:
                body = await self.fetcher(url)
                items = parse_feed(body, source_url=url)
                await self._upsert(items)
                total += len(items)
                self._log.info(
                    "refreshed {} ({} items)", _short_source(url), len(items),
                )
            except Exception as e:  # noqa: BLE001
                self._log.warning("feed {} failed: {}", _short_source(url), e)
        return total

    async def _upsert(self, items: list[dict]) -> None:
        if not items:
            return
        async with session_factory()() as s:
            for item in items:
                stmt = sqlite_insert(NewsItem).values(**item)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "ts": stmt.excluded.ts,
                        "content": stmt.excluded.content,
                        "author": stmt.excluded.author,
                        "url": stmt.excluded.url,
                    },
                )
                await s.execute(stmt)
            await s.commit()


# ──────────────────────────────────────────────────────────────────────
# Pure parsing
# ──────────────────────────────────────────────────────────────────────


def parse_feed(body: str, *, source_url: str = "") -> list[dict]:
    """Parse a single RSS 2.0 or Atom feed body into normalized dicts ready
    for the `news` table. Unknown shapes return [] rather than raising."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"feed is not valid XML: {e}") from e

    source = _short_source(source_url)
    out: list[dict] = []
    for entry in _iter_entries(root):
        fields = _entry_fields(entry)
        title = fields.get("title")
        if not title:
            continue
        url = fields.get("link") or fields.get("url")
        ts_ms = _parse_date(fields.get("pubDate") or fields.get("published") or "")
        if ts_ms is None:
            # Skip entries with no parseable timestamp — better than back-dating.
            continue
        sid = (
            fields.get("guid") or fields.get("id")
            or hashlib.sha1(f"{title}|{ts_ms}".encode()).hexdigest()[:16]
        )
        out.append({
            "source": source,
            "source_id": sid[:128],
            "ts": ts_ms,
            "author": fields.get("author") or fields.get("creator") or None,
            "content": _truncate(_clean(title), 600),
            "url": url,
        })
    return out


def _iter_entries(root: ET.Element):
    """Yield each entry/item element regardless of feed flavor."""
    # RSS 2.0: <rss><channel><item>
    for it in root.iter():
        if _strip_ns(it.tag) in ("item", "entry"):
            yield it


def _entry_fields(entry: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for child in entry:
        tag = _strip_ns(child.tag).lower()
        if tag == "link":
            href = child.attrib.get("href")
            text = (child.text or "").strip()
            out["link"] = href or text
        elif tag in ("title", "description", "summary", "content"):
            out[tag] = (child.text or "").strip()
        elif tag in ("pubdate", "published", "updated"):
            out["pubDate" if tag == "pubdate" else tag] = (child.text or "").strip()
        elif tag in ("guid", "id"):
            out["guid"] = (child.text or "").strip()
        elif tag in ("author", "creator", "dc:creator"):
            out["author"] = (child.text or "").strip()
    if "title" not in out and "summary" in out:
        out["title"] = out["summary"]
    return out


def _strip_ns(tag: str) -> str:
    return _NS_RE.sub("", tag)


def _parse_date(raw: str) -> Optional[int]:
    if not raw:
        return None
    # RFC 822 (RSS 2.0): "Mon, 24 May 2026 14:00:00 GMT"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO 8601 (Atom): "2026-05-24T14:00:00Z" or with offset
    try:
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def _clean(text: str) -> str:
    # Strip basic HTML and collapse whitespace.
    s = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", s).strip()


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _short_source(url: str) -> str:
    """A compact, stable source label per feed (e.g. "cointelegraph.com").
    Used as the `news.source` column so multiple feeds don't collide on
    `source_id`."""
    if not url:
        return "unknown"
    host = urlparse(url).netloc or url
    return host.removeprefix("www.")[:32]


# ──────────────────────────────────────────────────────────────────────
# Default fetcher
# ──────────────────────────────────────────────────────────────────────


async def _http_fetcher(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (cero personal-use crypto bot)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    }
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.text()
