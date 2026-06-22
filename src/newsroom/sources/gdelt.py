"""GDELT DOC 2.0 ingestion source.

Queries GDELT's global news index for recent articles at the AI x
policy/crypto intersection. The DOC ``artlist`` mode returns article metadata
(title, url, domain, seen-date) but no body snippet, so cleaned_text is built
from the title plus source domain.

  external_id = sha256(article url)  (urls are long and not always stable keys)
  cleaned_text = "{title} — {domain}"
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ._base import SourceItem, sha256_hex, upsert_items

SOURCE_CLASS = "gdelt"

log = logging.getLogger(__name__)

DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERY = "AI AND (regulation OR policy OR crypto OR blockchain)"
MAX_RECORDS = 25

# GDELT asks for roughly <=1 request / 5s.
RATE_LIMIT_S = 5.0
HTTP_TIMEOUT_S = 30.0


def _parse_seendate(value: str) -> datetime | None:
    """Parse GDELT's ``YYYYMMDDTHHMMSSZ`` seen-date into aware UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalize(articles: list[dict]) -> list[SourceItem]:
    items: list[SourceItem] = []
    seen: set[str] = set()
    for art in articles:
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        ext = sha256_hex(url)
        if ext in seen:  # GDELT can repeat the same url across pages
            continue
        seen.add(ext)

        domain = (art.get("domain") or "").strip()
        snippet = f"{title} — {domain}" if domain else title
        items.append(
            SourceItem(
                external_id=ext,
                url=url,
                title=title[:300],
                cleaned_text=snippet,
                published_at=_parse_seendate(art.get("seendate", "")),
                categories=[SOURCE_CLASS, *( [domain] if domain else [] )],
            )
        )
    return items


async def fetch() -> list[SourceItem]:
    """Run the GDELT DOC query and normalize the article list."""
    params = {
        "query": QUERY,
        "mode": "artlist",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "datedesc",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        try:
            resp = await client.get(DOC_URL, params=params)
            resp.raise_for_status()
            # GDELT sometimes returns an empty body or HTML error page; guard it.
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GDELT fetch failed: %s", exc)
            return []
    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not articles:
        return []
    await asyncio.sleep(RATE_LIMIT_S)
    return _normalize(articles)


async def ingest() -> tuple[int, int]:
    """Fetch GDELT articles and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
