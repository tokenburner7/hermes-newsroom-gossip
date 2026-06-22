"""Polymarket (Gamma API) ingestion source.

Polls Polymarket's public Gamma markets endpoint for high-volume markets and
keeps those whose question/tags touch AI, crypto or regulation. The Gamma API's
rate limits are undocumented, so we pace conservatively.

  external_id = market slug (falls back to id)
  cleaned_text = "{question} Current probability: {p}%. Volume: ${v}"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from ._base import SourceItem, upsert_items

SOURCE_CLASS = "polymarket"

log = logging.getLogger(__name__)

MARKETS_URL = "https://gamma-api.polymarket.com/markets"
QUERY_PARAMS = {"limit": 50, "volume_num_min": 10000, "closed": "false"}

# Case-insensitive topical filter applied to question text + tag labels. "ai"
# is word-boundary matched so it doesn't fire inside "rain"/"again"/"email";
# "crypto"/"regulation" stay substrings so "cryptocurrency"/"regulations" match.
_RELEVANCE_RE = re.compile(r"\bai\b|crypto|regulation", re.IGNORECASE)

# Undocumented limits -> be conservative between requests. We make a single
# request per run, so this mainly guards future pagination.
RATE_LIMIT_S = 5.0
HTTP_TIMEOUT_S = 30.0


def _is_relevant(question: str, tags: list[str]) -> bool:
    """True if a topical keyword appears in the question or any tag."""
    haystack = question + " " + " ".join(tags)
    return bool(_RELEVANCE_RE.search(haystack))


def _tag_labels(market: dict) -> list[str]:
    """Extract tag label strings from a Gamma market's ``tags``/``events``.

    Gamma returns tags either as a list of dicts (``{"label": ...}``) or, on the
    market's parent event, as a similar list. We coalesce whatever is present.
    """
    labels: list[str] = []
    for tag in market.get("tags") or []:
        if isinstance(tag, dict):
            label = tag.get("label") or tag.get("slug")
            if label:
                labels.append(str(label))
        elif isinstance(tag, str):
            labels.append(tag)
    return labels


def _yes_probability(market: dict) -> float | None:
    """Best-effort 'Yes' probability (0-100) from Gamma ``outcomePrices``.

    ``outcomePrices`` is a JSON-encoded string like ``'["0.62", "0.38"]'``; the
    first entry is the Yes price. Returns None when unparseable.
    """
    raw = market.get("outcomePrices")
    if raw is None:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return round(float(prices[0]) * 100, 1)
    except (ValueError, TypeError, IndexError):
        return None
    return None


def _parse_dt(value: str | None) -> datetime | None:
    """Parse a Gamma ISO-8601 timestamp (e.g. endDate) to aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize(markets: list[dict]) -> list[SourceItem]:
    items: list[SourceItem] = []
    for market in markets:
        question = (market.get("question") or "").strip()
        if not question:
            continue
        tags = _tag_labels(market)
        if not _is_relevant(question, tags):
            continue

        slug = market.get("slug") or str(market.get("id") or "")
        if not slug:
            continue

        prob = _yes_probability(market)
        prob_str = f"{prob}%" if prob is not None else "n/a"
        try:
            volume = float(market.get("volumeNum") or market.get("volume") or 0)
        except (ValueError, TypeError):
            volume = 0.0

        items.append(
            SourceItem(
                external_id=slug,
                url=f"https://polymarket.com/market/{slug}",
                title=question[:200],
                cleaned_text=(
                    f"{question} Current probability: {prob_str}. "
                    f"Volume: ${volume:,.0f}"
                ),
                published_at=_parse_dt(market.get("endDate")),
                categories=[SOURCE_CLASS, *tags[:5]],
            )
        )
    return items


async def fetch() -> list[SourceItem]:
    """Fetch high-volume markets and keep the topically relevant ones."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        try:
            resp = await client.get(MARKETS_URL, params=QUERY_PARAMS)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Polymarket fetch failed: %s", exc)
            return []
    # Gamma may return a bare list or {"data": [...]} depending on params.
    markets = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(markets, list):
        log.warning("Polymarket returned unexpected payload type: %r", type(payload))
        return []
    await asyncio.sleep(RATE_LIMIT_S)
    return _normalize(markets)


async def ingest() -> tuple[int, int]:
    """Fetch relevant Polymarket markets and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
