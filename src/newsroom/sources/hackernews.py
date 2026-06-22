"""Hacker News (Firebase API) ingestion source.

Pulls the current top stories and keeps those whose title mentions an AI or
crypto keyword. The Firebase API is auth-free and unthrottled; we still pause
briefly between item fetches to be polite.

The API exposes no article body, so cleaned_text is the title plus the linked
URL (the story's own discussion URL when it is a text/Ask HN post).

  external_id = HN story id (as a string)
  cleaned_text = "{title} {url}"
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from ._base import SourceItem, upsert_items

SOURCE_CLASS = "hackernews"

log = logging.getLogger(__name__)

TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
ITEM_PAGE_URL = "https://news.ycombinator.com/item?id={id}"

TOP_N = 30

# Word-boundary matched so "ai" doesn't fire on "said"/"rain", "tee" on
# "committee", etc.
KEYWORDS = (
    "ai", "llm", "gpt", "crypto", "blockchain", "defi",
    "ethereum", "bitcoin", "token", "agent", "zk", "tee",
)
_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.IGNORECASE
)

RATE_LIMIT_S = 0.1
HTTP_TIMEOUT_S = 20.0


def _is_relevant(title: str) -> bool:
    return bool(_KEYWORD_RE.search(title))


def _to_item(story: dict) -> SourceItem | None:
    title = (story.get("title") or "").strip()
    story_id = story.get("id")
    if not title or story_id is None:
        return None
    if not _is_relevant(title):
        return None

    # Link posts carry ``url``; Ask/Show-HN text posts only have a discussion.
    page = ITEM_PAGE_URL.format(id=story_id)
    url = story.get("url") or page
    published = story.get("time")
    published_at = (
        datetime.fromtimestamp(published, tz=timezone.utc)
        if isinstance(published, (int, float))
        else None
    )
    return SourceItem(
        external_id=str(story_id),
        url=url,
        title=title,
        cleaned_text=f"{title} {url}",
        published_at=published_at,
        categories=[SOURCE_CLASS],
    )


async def fetch() -> list[SourceItem]:
    """Fetch the top stories and keep AI/crypto-relevant titles."""
    items: list[SourceItem] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        try:
            resp = await client.get(TOP_STORIES_URL)
            resp.raise_for_status()
            ids = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("HN top stories fetch failed: %s", exc)
            return []
        if not isinstance(ids, list):
            return []

        for story_id in ids[:TOP_N]:
            try:
                r = await client.get(ITEM_URL.format(id=story_id))
                r.raise_for_status()
                story = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("HN item %s fetch failed: %s", story_id, exc)
                await asyncio.sleep(RATE_LIMIT_S)
                continue
            if isinstance(story, dict):
                item = _to_item(story)
                if item is not None:
                    items.append(item)
            await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch top HN stories and upsert relevant ones. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
