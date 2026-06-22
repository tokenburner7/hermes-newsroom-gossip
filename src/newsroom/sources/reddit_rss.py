"""Reddit (public RSS/Atom) ingestion source.

Reads each subreddit's auth-free ``.rss`` feed and upserts recent posts. Reddit
serves these as Atom; we fetch the bytes with httpx (Reddit 429s the default
client UA, so we send ``settings.http_user_agent``) and parse with feedparser
off the event loop.

  external_id = Reddit fullname id (e.g. "t3_abc123")
  cleaned_text = "{title} — {selftext snippet}"
"""

from __future__ import annotations

import asyncio
import logging
import re
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import settings
from ._base import SourceItem, sha256_hex, upsert_items

SOURCE_CLASS = "reddit"

log = logging.getLogger(__name__)

SUBREDDITS = ("popculturechat", "Fauxmoi", "Deuxmoi", "entertainment", "popculture")
FEED_URL = "https://www.reddit.com/r/{sub}/.rss"

RATE_LIMIT_S = 1.0
HTTP_TIMEOUT_S = 30.0
SNIPPET_CHARS = 500

# Reddit aggressively 429s RSS clients. Retry with exponential backoff before
# giving up (delays applied *before* each retry) (F4).
RETRY_BACKOFF_S: tuple[float, ...] = (5.0, 15.0, 45.0)

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


def _entry_id(entry, sub: str) -> str:
    """Reddit Atom ids are the post fullname (``t3_...``); fall back to a hash."""
    raw = getattr(entry, "id", "") or getattr(entry, "link", "")
    if raw.startswith("t3_"):
        return raw
    # Some feeds wrap the id as a tag URI; keep the trailing fullname if present.
    m = re.search(r"t3_[0-9a-z]+", raw, re.IGNORECASE)
    if m:
        return m.group(0)
    return f"{sub}:{sha256_hex(raw)[:16]}" if raw else f"{sub}:unknown"


def _normalize(sub: str, parsed: "feedparser.FeedParserDict") -> list[SourceItem]:
    items: list[SourceItem] = []
    for entry in parsed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        snippet = _strip_html(getattr(entry, "summary", ""))[:SNIPPET_CHARS]
        cleaned = f"{title} — {snippet}" if snippet else title
        items.append(
            SourceItem(
                external_id=_entry_id(entry, sub),
                url=link,
                title=title,
                cleaned_text=cleaned,
                published_at=_entry_published(entry),
                categories=[SOURCE_CLASS, f"r/{sub}"],
            )
        )
    return items


async def _fetch_sub(client: httpx.AsyncClient, sub: str) -> list[SourceItem]:
    url = FEED_URL.format(sub=sub)
    last_exc: Exception | None = None
    for attempt, delay_s in enumerate([0.0, *RETRY_BACKOFF_S], 1):
        if delay_s > 0:
            log.info("Reddit r/%s: backoff %.0fs (attempt %d)", sub, delay_s, attempt)
            await asyncio.sleep(delay_s)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and attempt <= len(RETRY_BACKOFF_S):
                last_exc = exc
                continue
            log.warning("Reddit fetch failed for r/%s: %s", sub, exc)
            return []
        except httpx.HTTPError as exc:
            log.warning("Reddit fetch failed for r/%s: %s", sub, exc)
            return []
        else:
            break  # success — exit retry loop
    else:
        # All retries exhausted on 429
        log.warning("Reddit fetch failed for r/%s after %d retries: %s", sub, len(RETRY_BACKOFF_S) + 1, last_exc)
        return []
    # feedparser is synchronous; keep it off the event loop.
    parsed = await asyncio.to_thread(feedparser.parse, resp.content)
    if parsed.bozo and not parsed.entries:
        log.warning("Reddit feed for r/%s did not parse: %s", sub, parsed.bozo_exception)
        return []
    return _normalize(sub, parsed)


async def fetch() -> list[SourceItem]:
    """Fetch and parse each subreddit feed, paced between requests."""
    headers = {
        "User-Agent": settings.http_user_agent,
        "Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    items: list[SourceItem] = []
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S, headers=headers, follow_redirects=True
    ) as client:
        for sub in SUBREDDITS:
            items.extend(await _fetch_sub(client, sub))
            await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch subreddit RSS posts and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
