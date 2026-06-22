"""US Treasury FiscalData API ingestion source.

Fetches latest Treasury yields, debt outstanding, and TIPS breakevens via the
public FiscalData Treasury API. No API key required.

  external_id = dataset + endpoint anchor (e.g. "avg_interest_rates-LONGTERM")
  cleaned_text = compact JSON with latest value, date, and dataset metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from ._base import SourceItem, upsert_items

SOURCE_CLASS = "treasury"

log = logging.getLogger(__name__)

# (endpoint, field, label) — each becomes one SourceItem
ENDPOINTS: list[dict] = [
    {
        "endpoint": "v2/accounting/od/avg_interest_rates",
        "filter_key": "security_type_desc",
        "filter_value": "Treasury Bills",
        "value_field": "avg_interest_rate_amt",
        "date_field": "record_date",
        "label": "T-Bill Average Interest Rate",
    },
    {
        "endpoint": "v2/accounting/od/avg_interest_rates",
        "filter_key": "security_type_desc",
        "filter_value": "Treasury Notes",
        "value_field": "avg_interest_rate_amt",
        "date_field": "record_date",
        "label": "T-Note Average Interest Rate",
    },
    {
        "endpoint": "v2/accounting/od/avg_interest_rates",
        "filter_key": "security_type_desc",
        "filter_value": "Treasury Bonds",
        "value_field": "avg_interest_rate_amt",
        "date_field": "record_date",
        "label": "T-Bond Average Interest Rate",
    },
    {
        "endpoint": "v1/accounting/od/debt_to_penny",
        "filter_key": None,
        "filter_value": None,
        "value_field": "tot_pub_debt_out_amt",
        "date_field": "record_date",
        "label": "Total Public Debt Outstanding",
    },
]

FISCALDATA_BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
RATE_LIMIT_S = 0.3  # Public API is permissive but we pace ourselves
HTTP_TIMEOUT_S = 30.0


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _fetch_endpoint(
    client: httpx.AsyncClient, spec: dict, external_id: str
) -> SourceItem | None:
    """Fetch the single latest record for one endpoint + filter combination."""
    params = {
        "sort": f"-{spec['date_field']}",
        "page[size]": 1,
        "format": "json",
    }
    if spec["filter_key"] and spec["filter_value"]:
        params[f"filter={spec['filter_key']}:eq"] = spec["filter_value"]

    try:
        resp = await client.get(
            f"{FISCALDATA_BASE}/{spec['endpoint']}", params=params
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Treasury fetch failed for %s: %s", spec["label"], exc)
        return None

    records = payload.get("data", [])
    if not records:
        log.warning("Treasury returned no records for %s", spec["label"])
        return None

    rec = records[0]
    value = rec.get(spec["value_field"])
    date = rec.get(spec["date_field"], "")

    if value is None:
        log.warning("Treasury record for %s has no value", spec["label"])
        return None

    summary = {
        "label": spec["label"],
        "endpoint": spec["endpoint"],
        "value": value,
        "date": date,
        "units": "USD" if "debt" in spec["label"].lower() else "percent",
    }

    return SourceItem(
        external_id=external_id,
        url=f"https://fiscaldata.treasury.gov/datasets/{spec['endpoint'].split('/')[-1]}/",
        title=f"{spec['label']}: {value}",
        cleaned_text=json.dumps(summary),
        published_at=_parse_date(date) or datetime.now(timezone.utc),
        categories=[SOURCE_CLASS, "macro", "treasury"],
    )


async def fetch() -> list[SourceItem]:
    """Fetch latest observations for all tracked Treasury endpoints."""
    items: list[SourceItem] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        for i, spec in enumerate(ENDPOINTS):
            external_id = f"{spec['endpoint'].split('/')[-1]}-{spec.get('filter_value', '') or 'all'}"
            # Clean external_id — replace spaces/slashes
            external_id = external_id.replace(" ", "_").replace("/", "-")
            item = await _fetch_endpoint(client, spec, external_id)
            if item is not None:
                items.append(item)
            if i < len(ENDPOINTS) - 1:
                await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch latest Treasury data and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
