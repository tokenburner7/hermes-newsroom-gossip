"""BLS (Bureau of Labor Statistics) economic-data ingestion source.

Fetches the latest observation for CPI, unemployment, PPI, and payrolls via the
BLS Public Data API v2. Requires ``settings.bls_api_key``; without it the source
no-ops gracefully.

  external_id = BLS series id (e.g. "CUSR0000SA0")
  cleaned_text = compact JSON summary of latest value, date, and series name
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ._base import SourceItem, upsert_items

SOURCE_CLASS = "bls"

log = logging.getLogger(__name__)

# BLS series ids → human-readable label
SERIES: dict[str, str] = {
    "CUSR0000SA0": "CPI-U All Items (Headline CPI)",
    "CUSR0000SA0L1E": "CPI-U Core (All Items Less Food & Energy)",
    "LNS14000000": "Unemployment Rate (U-3)",
    "CES0000000001": "Total Nonfarm Payrolls",
    "PCUOMFG--OMFG--": "PPI Final Demand",
}

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
# BLS rate limit: ~25 req/day on unregistered, 500/day with key. We batch all
# series into a single POST, so one req per ingest.
RATE_LIMIT_S = 1.0
HTTP_TIMEOUT_S = 30.0


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def fetch() -> list[SourceItem]:
    """Fetch latest observation for all tracked BLS series in one POST."""
    if not settings.bls_api_key:
        log.warning("BLS_API_KEY not set; skipping bls source.")
        return []

    payload = {
        "seriesid": list(SERIES.keys()),
        "startyear": str(datetime.now(timezone.utc).year - 1),
        "endyear": str(datetime.now(timezone.utc).year),
        "registrationkey": settings.bls_api_key,
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        try:
            resp = await client.post(BLS_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("BLS fetch failed: %s", exc)
            return []

    if data.get("status") != "REQUEST_SUCCEEDED":
        log.warning("BLS API returned non-success status: %s", data.get("status"))
        return []

    items: list[SourceItem] = []
    for series in data.get("Results", {}).get("series", []):
        series_id = series.get("seriesID", "")
        name = SERIES.get(series_id, series_id)
        observations = series.get("data", [])
        if not observations:
            continue

        # Latest observation (API returns newest first)
        latest = observations[0]
        value = latest.get("value", "")
        period = latest.get("periodName", "")  # e.g. "June"
        year = latest.get("year", "")
        date_str = f"{year}-{latest.get('period', '').replace('M', '')}-01"

        summary = {
            "series_id": series_id,
            "name": name,
            "latest_value": value,
            "period": period,
            "year": year,
            "units": series.get("data", [{}])[0].get("footnotes", [{}]),
        }

        items.append(
            SourceItem(
                external_id=series_id,
                url=f"https://data.bls.gov/timeseries/{series_id}",
                title=f"{name}: {value} ({period} {year})",
                cleaned_text=json.dumps(summary),
                published_at=_parse_date(date_str) or datetime.now(timezone.utc),
                categories=[SOURCE_CLASS, "macro", "economic-indicator"],
            )
        )

    await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch latest BLS observations and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
