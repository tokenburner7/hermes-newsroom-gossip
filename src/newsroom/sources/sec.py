"""SEC EDGAR ingestion source.

Pulls each tracked company's *recent submissions* feed from EDGAR's JSON API and
upserts material filings (10-K, 10-Q, 8-K, S-1) into ``sources``.

EDGAR requires a descriptive ``User-Agent`` (see ``settings.sec_user_agent``) or
it answers 403, and asks callers to stay **<=10 requests/second** (fair-access).
We issue one request per CIK with a small pacing sleep, well under that ceiling.

  external_id = "{cik}/{form}/{accession_number}"
  cleaned_text = "{company} filed {form} on {date}. {description}"
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ._base import SourceItem, upsert_items

SOURCE_CLASS = "sec"

log = logging.getLogger(__name__)

# CIK (zero-padded to 10 digits) -> human label. EDGAR pads CIKs in the URL but
# reports them unpadded in the body; we key external_id on the padded form.
# Ripple Labs is privately held and does not file with the SEC, so we track
# Marathon Digital — a public crypto-treasury filer — as a related proxy.
CIKS: dict[str, str] = {
    "0001679788": "Coinbase Global, Inc.",
    "0001050446": "MicroStrategy Incorporated",
    "0001507605": "Marathon Digital Holdings, Inc.",
}

# Material filing forms we care about (others, e.g. Form 4 insider trades, are
# skipped as noise for a research newsroom).
WANTED_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "8-K", "S-1"})

# Cap rows per company so a chatty 8-K filer cannot dominate one run.
MAX_FILINGS_PER_CIK = 20

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# <=10 req/s fair-access limit; one request per CIK with a comfortable margin.
RATE_LIMIT_S = 0.15
HTTP_TIMEOUT_S = 30.0


def _filing_url(cik: str, accession: str, primary_doc: str | None) -> str:
    """Build the canonical EDGAR archive URL for a filing.

    ``cik`` is the unpadded integer in the path; ``accession`` is dash-stripped
    for the directory. Links to the primary document when known, else the index.
    """
    cik_int = str(int(cik))
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
    if primary_doc:
        return f"{base}/{primary_doc}"
    return f"{base}/{accession}-index.htm"


def _parse_date(value: str) -> datetime | None:
    """Parse an EDGAR ``YYYY-MM-DD`` filing date into a UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalize(cik: str, company: str, payload: dict) -> list[SourceItem]:
    """Turn a submissions payload into SourceItems for the wanted forms."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    descs = recent.get("primaryDocDescription") or []
    docs = recent.get("primaryDocument") or []

    # EDGAR uses the body name when present; fall back to our static label.
    company = payload.get("name") or company

    items: list[SourceItem] = []
    for i, form in enumerate(forms):
        if len(items) >= MAX_FILINGS_PER_CIK:
            break
        if form not in WANTED_FORMS:
            continue
        accession = accessions[i] if i < len(accessions) else ""
        if not accession:
            continue
        filing_date = dates[i] if i < len(dates) else ""
        description = (descs[i] if i < len(descs) else "") or form
        primary_doc = docs[i] if i < len(docs) else None

        items.append(
            SourceItem(
                external_id=f"{cik}/{form}/{accession}",
                url=_filing_url(cik, accession, primary_doc),
                title=f"{company} — {form} ({filing_date})",
                cleaned_text=(
                    f"{company} filed {form} on {filing_date}. {description}"
                ),
                published_at=_parse_date(filing_date),
                categories=[SOURCE_CLASS, form],
            )
        )
    return items


async def fetch() -> list[SourceItem]:
    """Fetch recent filings for every tracked CIK, paced under the rate limit."""
    headers = {
        "User-Agent": settings.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }
    items: list[SourceItem] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
        for cik, company in CIKS.items():
            url = SUBMISSIONS_URL.format(cik=cik)
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("SEC fetch failed for CIK %s (%s): %s", cik, company, exc)
                await asyncio.sleep(RATE_LIMIT_S)
                continue
            items.extend(_normalize(cik, company, payload))
            await asyncio.sleep(RATE_LIMIT_S)
    return items


async def ingest() -> tuple[int, int]:
    """Fetch recent SEC filings and upsert them. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
