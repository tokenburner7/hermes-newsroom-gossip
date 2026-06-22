"""CoinGecko (simple price) ingestion source.

Snapshots USD spot price + 24h change for a basket of AI/crypto tokens via the
``/simple/price`` endpoint. A single request returns the whole basket; we emit
one ``sources`` row per coin so each can be selected/cited independently.

  external_id = CoinGecko coin id (e.g. "bittensor")
  cleaned_text = compact JSON summary of price and 24h change
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ._base import SourceItem, upsert_items

SOURCE_CLASS = "coingecko"

log = logging.getLogger(__name__)

# AI x crypto basket + the two majors as macro anchors.
COIN_IDS = [
    "fetch-ai",
    "singularitynet",
    "ocean-protocol",
    "bittensor",
    "render-token",
    "akash-network",
    "worldcoin-wld",
    "arweave",
    "near",
    "bitcoin",
    "ethereum",
]

SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
# ~30 calls/min on the demo tier; one call per run with margin to spare.
RATE_LIMIT_S = 2.0
HTTP_TIMEOUT_S = 30.0


def _normalize(prices: dict, captured_at: datetime) -> list[SourceItem]:
    items: list[SourceItem] = []
    for coin_id in COIN_IDS:
        data = prices.get(coin_id)
        if not isinstance(data, dict) or "usd" not in data:
            continue
        usd = data.get("usd")
        change = data.get("usd_24h_change")
        change_str = f"{change:+.2f}%" if isinstance(change, (int, float)) else "n/a"

        summary = {
            "id": coin_id,
            "usd": usd,
            "usd_24h_change": (
                round(change, 4) if isinstance(change, (int, float)) else None
            ),
            "as_of": captured_at.isoformat(),
        }
        items.append(
            SourceItem(
                external_id=coin_id,
                url=f"https://www.coingecko.com/en/coins/{coin_id}",
                title=f"{coin_id} — ${usd} ({change_str} 24h)",
                cleaned_text=json.dumps(summary),
                published_at=captured_at,
                categories=[SOURCE_CLASS, "price"],
            )
        )
    return items


async def fetch() -> list[SourceItem]:
    """Fetch the basket's spot prices and 24h changes in one request."""
    params = {
        "ids": ",".join(COIN_IDS),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    headers: dict[str, str] = {}
    if settings.coingecko_api_key:
        # Demo-tier auth header (Pro tier uses ``x-cg-pro-api-key``).
        headers["x-cg-demo-api-key"] = settings.coingecko_api_key

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
        try:
            resp = await client.get(SIMPLE_PRICE_URL, params=params)
            resp.raise_for_status()
            prices = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("CoinGecko fetch failed: %s", exc)
            return []
    await asyncio.sleep(RATE_LIMIT_S)
    return _normalize(prices, datetime.now(timezone.utc))


async def ingest() -> tuple[int, int]:
    """Snapshot token prices and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
