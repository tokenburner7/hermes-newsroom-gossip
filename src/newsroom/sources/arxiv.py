"""arXiv ingestion source (Phase 0, Day 1).

Polls the arXiv API for recent papers in ``cs.AI``, ``cs.CR`` and ``cs.LG``,
respecting arXiv's Terms of Service (**<=1 request / 3s on a single
connection**), and upserts them into the ``sources`` table keyed by
``(source_class, external_id)`` with ``url_hash = sha256(url)``.

Day-1 scope stores the *abstract* in ``cleaned_text`` as a fallback. Day-2 adds
full-text PDF extraction (PyMuPDF) that overwrites ``cleaned_text``.

The third-party ``arxiv`` package is imported absolutely; it does not collide
with this module, whose fully-qualified name is ``newsroom.sources.arxiv``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import arxiv
import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import settings
from ..db import async_session_factory
from ..models import Source

SOURCE_CLASS = "arxiv"

log = logging.getLogger(__name__)

# Matches a trailing arXiv version suffix, e.g. "2401.12345v3" -> "2401.12345".
_VERSION_RE = re.compile(r"v\d+$")
# "1d", "12h", "2w", "30m", "90s" -> timedelta.
_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_SINCE_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def sha256_hex(text: str) -> str:
    """Return the hex SHA-256 of ``text`` (used for ``url_hash``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_since(spec: str) -> timedelta:
    """Parse a duration spec like ``"1d"``, ``"12h"``, ``"2w"`` to a timedelta.

    Raises ``ValueError`` on malformed input.
    """
    m = _SINCE_RE.match(spec)
    if not m:
        raise ValueError(
            f"invalid --since value {spec!r}; expected e.g. '1d', '12h', '2w'"
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(**{_SINCE_UNITS[unit]: value})


def canonical_id(short_id: str) -> str:
    """Strip the version suffix so revisions map to one stable ``external_id``."""
    return _VERSION_RE.sub("", short_id)


def abs_url(external_id: str) -> str:
    """Canonical (versionless) abstract URL for a paper id."""
    return f"https://arxiv.org/abs/{external_id}"


@dataclass(slots=True)
class ArxivPaper:
    """Normalized view of an arXiv result, ready for upsert."""

    external_id: str
    url: str
    url_hash: str
    title: str
    abstract: str
    published_at: datetime
    updated_at: datetime
    categories: list[str]
    pdf_url: str | None
    # Filled by enrich_with_full_text(); None until (and unless) PDF extraction
    # succeeds. upsert prefers this over the abstract for cleaned_text.
    full_text: str | None = field(default=None)

    @classmethod
    def from_result(cls, r: "arxiv.Result") -> "ArxivPaper":
        ext = canonical_id(r.get_short_id())
        url = abs_url(ext)
        return cls(
            external_id=ext,
            url=url,
            url_hash=sha256_hex(url),
            title=(r.title or "").strip(),
            abstract=(r.summary or "").strip(),
            published_at=r.published,
            updated_at=r.updated,
            categories=list(r.categories or []),
            pdf_url=r.pdf_url,
        )


def _build_client() -> "arxiv.Client":
    """An arXiv client that honors the rate limit on a single connection.

    ``delay_seconds`` enforces the <=1 req / 3s pacing between page fetches;
    the underlying ``requests.Session`` keeps a single connection alive.
    """
    return arxiv.Client(
        page_size=settings.arxiv_page_size,
        delay_seconds=settings.arxiv_min_interval_s,
        num_retries=3,
    )


def fetch_recent(since: timedelta, *, max_results: int | None = None) -> list[ArxivPaper]:
    """Fetch papers submitted within ``since`` of now (newest first).

    Synchronous (the ``arxiv`` lib uses ``requests``). Results are sorted by
    submission date descending, so we stop as soon as we pass the cutoff.
    """
    cutoff = datetime.now(timezone.utc) - since
    limit = max_results if max_results is not None else settings.arxiv_max_results

    search = arxiv.Search(
        query=settings.arxiv_query,
        max_results=limit,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )
    client = _build_client()

    papers: list[ArxivPaper] = []
    for result in client.results(search):
        if result.published < cutoff:
            break  # sorted desc -> everything after is older
        papers.append(ArxivPaper.from_result(result))
    return papers


_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _clean_text(raw: str) -> str:
    """Normalize extracted PDF text: collapse runs of spaces and blank lines.

    PyMuPDF preserves layout-driven spacing that is noisy for an LLM. We keep
    paragraph structure (single blank line) but squeeze the rest.
    """
    out = _WHITESPACE_RE.sub(" ", raw)
    out = _BLANKLINES_RE.sub("\n\n", out)
    # Strip trailing spaces on each line.
    out = "\n".join(line.rstrip() for line in out.splitlines())
    return out.strip()


def extract_pdf_text(
    pdf_url: str,
    *,
    max_chars: int | None = None,
    timeout_s: float | None = None,
) -> str | None:
    """Download a paper PDF and extract its body text with PyMuPDF (fitz).

    Returns cleaned text truncated to ``max_chars``, or ``None`` if the download
    or extraction fails (caller falls back to the abstract). Never raises.
    """
    max_chars = max_chars if max_chars is not None else settings.pdf_max_chars
    timeout_s = timeout_s if timeout_s is not None else settings.pdf_download_timeout_s

    try:
        import fitz  # PyMuPDF; imported lazily (heavy native extension)

        headers = {"User-Agent": settings.http_user_agent}
        with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
            resp = client.get(pdf_url)
            resp.raise_for_status()
            data = resp.content

        if not data:
            log.warning("empty PDF body for %s", pdf_url)
            return None

        parts: list[str] = []
        total = 0
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                parts.append(page.get_text("text"))
                total += len(parts[-1])
                # Stop early once we clearly have more than we will keep.
                if total >= max_chars * 2:
                    break

        cleaned = _clean_text("\n".join(parts))
        if not cleaned:
            log.warning("no extractable text in PDF %s", pdf_url)
            return None
        return cleaned[:max_chars]
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        log.warning("PDF extraction failed for %s: %s", pdf_url, exc)
        return None


def enrich_with_full_text(
    papers: list[ArxivPaper],
    *,
    max_chars: int | None = None,
    min_interval_s: float | None = None,
) -> int:
    """Populate ``full_text`` on each paper from its PDF, paced to the rate limit.

    Mutates ``papers`` in place. Returns the count that extracted successfully.
    Downloads are serialized with a ``>=min_interval_s`` gap (arXiv ToS: <=1
    request / 3s on a single connection). Synchronous; run via ``to_thread``.
    """
    interval = min_interval_s if min_interval_s is not None else settings.arxiv_min_interval_s
    ok = 0
    last = 0.0
    for paper in papers:
        if not paper.pdf_url:
            continue
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        text = extract_pdf_text(paper.pdf_url, max_chars=max_chars)
        if text:
            paper.full_text = text
            ok += 1
    return ok


async def upsert_papers(papers: list[ArxivPaper]) -> int:
    """Upsert papers into ``sources`` on ``(source_class, external_id)``.

    Returns the number of rows processed. Stores full text in ``cleaned_text``
    when available (see :func:`enrich_with_full_text`), else the abstract.
    """
    if not papers:
        return 0

    now = datetime.now(timezone.utc)
    rows = [
        {
            "source_class": SOURCE_CLASS,
            "external_id": p.external_id,
            "url": p.url,
            "title": p.title,
            "categories": p.categories,
            # Full text when extracted, else the abstract (Day-1 fallback).
            "cleaned_text": p.full_text or p.abstract,
            "published_at": p.published_at,
            "retrieved_at": now,
            "license_flag": "summarize_only",
            "scrape_allowed": True,
            "url_hash": p.url_hash,
        }
        for p in papers
    ]

    stmt = pg_insert(Source).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_class", "external_id"],
        set_={
            "url": stmt.excluded.url,
            "title": stmt.excluded.title,
            "categories": stmt.excluded.categories,
            "cleaned_text": stmt.excluded.cleaned_text,
            "published_at": stmt.excluded.published_at,
            "retrieved_at": stmt.excluded.retrieved_at,
            "url_hash": stmt.excluded.url_hash,
        },
    )

    async with async_session_factory() as session:
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def ingest(
    since: timedelta,
    *,
    max_results: int | None = None,
    full_text: bool = False,
) -> tuple[int, int]:
    """Fetch recent papers and upsert them. Returns ``(fetched, upserted)``.

    With ``full_text=True`` each paper's PDF is downloaded and its body text
    extracted (PyMuPDF), paced to the arXiv rate limit, falling back to the
    abstract per paper on failure. The blocking arXiv fetch + PDF extraction run
    in a worker thread so this coroutine plays nicely inside an event loop.
    """
    papers = await asyncio.to_thread(fetch_recent, since, max_results=max_results)
    if full_text and papers:
        await asyncio.to_thread(enrich_with_full_text, papers)
    upserted = await upsert_papers(papers)
    return len(papers), upserted
