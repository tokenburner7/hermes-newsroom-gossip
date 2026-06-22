"""FRED (St. Louis Fed) economic-data ingestion source.

Fetches the latest observation for a handful of macro series that frame the
AI x crypto narrative (policy rate, inflation, the dollar index, GDP). Requires
``settings.fred_api_key``; without it the source no-ops gracefully.

  external_id = FRED series id (e.g. "FEDFUNDS")
  cleaned_text = "Series: {name}. Latest value: {value} as of {date}"
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ._base import SourceItem, upsert_items

SOURCE_CLASS = "fred"

log = logging.getLogger(__name__)

# series_id -> human-readable name (used in cleaned_text/title).
SERIES: dict[str, str] = {
    "FEDFUNDS": "Effective Federal Funds Rate",
    "CPIAUCSL": "CPI for All Urban Consumers (All Items)",
    "DTWEXBGS": "Nominal Broad U.S. Dollar Index",
    "GDP": "Gross Domestic Product",
}

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
# 120 req/min ceiling -> 0.5s between series keeps us comfortably under.
RATE_LIMIT_S = 0.5
HTTP_TIMEOUT_S = 30.0


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _fetch_series(client: httpx.AsyncClient, series_id: str, name: str) -> SourceItem | None:
    """Fetch the single latest observation for one series."""
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "limit": 1,
        "sort_order": "desc",
    }
    try:
        resp = await client.get(OBSERVATIONS_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("FRED fetch failed for %s: %s", series_id, exc)
        return None

    observations = payload.get("observations") or []
    if not observations:
        log.warning("FRED returned no observations for %s", series_id)
        return None

    obs = observations[0]
    value = obs.get("value")
    date = obs.get("date", "")
    # FRED encodes missing values as ".".
    if value in (None, "", "."):
        log.warning("FRED latest observation for %s is missing", series_id)
        return None

    return SourceItem(
        external_id=series_id,
        url=f"https://fred.stlouisfed.org/series/{series_id}",
        title=f"{name} ({series_id}): {value}",
        cleaned_text=f"Series: {name}. Latest value: {value} as of {date}",
        published_at=_parse_date(date),
        categories=[SOURCE_CLASS, "macro"],
    )


async def fetch() -> list[SourceItem]:
    """Fetch the latest observation for each tracked series."""
    if not settings.fred_api_key:
        log.warning("FRED_API_KEY not set; skipping fred source.")
        return []

    items: list[SourceItem] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        for series_id, name in SERIES.items():
            item = await _fetch_series(client, series_id, name)
            if item is not None:
                items.append(item)
            await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch latest macro observations and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
