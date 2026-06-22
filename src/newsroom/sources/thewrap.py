"""TheWrap (Hollywood business & entertainment RSS) ingestion source.

Reads TheWrap's public RSS feed and upserts recent posts. We fetch the bytes
with httpx (sending ``settings.http_user_agent``) and parse with feedparser off
the event loop, exactly like ``reddit_rss.py``.

  external_id = entry.id (falls back to the article URL)
  cleaned_text = "{title} — {summary snippet}"
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from calendar import timegm
from datetime import datetime, timezone

import feedparser

from ._base import SourceItem, upsert_items

SOURCE_CLASS = "thewrap"

log = logging.getLogger(__name__)

FEED_URL = "https://www.thewrap.com/feed/"

HTTP_TIMEOUT_S = 30.0
SNIPPET_CHARS = 500

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str | None) -> str:
    """Collapse an HTML summary fragment to a plain-text snippet."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", text).strip()


def _entry_published(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed is None:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def _normalize(parsed: "feedparser.FeedParserDict") -> list[SourceItem]:
    items: list[SourceItem] = []
    for entry in parsed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        eid = getattr(entry, "id", "") or link
        summary = (
            getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        )
        snippet = _strip_html(summary)[:SNIPPET_CHARS]
        cleaned = f"{title} — {snippet}" if snippet else title
        items.append(
            SourceItem(
                external_id=eid,
                url=link,
                title=title,
                cleaned_text=cleaned,
                published_at=_entry_published(entry),
                categories=[SOURCE_CLASS],
            )
        )
    return items


async def fetch() -> list[SourceItem]:
    """Fetch via curl (avoids Cloudflare bot detection on httpx) and parse."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "-H", "Accept: application/rss+xml,text/html,*/*",
            "--max-time", str(HTTP_TIMEOUT_S),
            FEED_URL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            log.warning("%s fetch failed: curl exit %d", SOURCE_CLASS, proc.returncode)
            return []
    except Exception as exc:
        log.warning("%s fetch failed: %s", SOURCE_CLASS, exc)
        return []

    parsed = await asyncio.to_thread(feedparser.parse, stdout)
    if parsed.bozo and not parsed.entries:
        log.warning("%s feed did not parse: %s", SOURCE_CLASS, parsed.bozo_exception)
        return []
    return _normalize(parsed)


async def ingest() -> tuple[int, int]:
    """Fetch RSS items and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
