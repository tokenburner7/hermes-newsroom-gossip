"""Shared helpers for non-arXiv ingestion sources.

Every source normalizes its upstream payload into a list of :class:`SourceItem`
and hands them to :func:`upsert_items`, which writes the §3.3 ``sources`` table
keyed by ``(source_class, external_id)`` with ``url_hash = sha256(url)`` — the
exact upsert shape ``sources/arxiv.py`` uses, factored out so each source module
stays focused on *fetch -> normalize*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import async_session_factory
from ..models import Source


def sha256_hex(text: str) -> str:
    """Return the hex SHA-256 of ``text`` (used for ``url_hash``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class SourceItem:
    """A normalized item ready to upsert into ``sources``.

    ``cleaned_text`` is what downstream selection/research reads, so each source
    composes a compact, self-describing summary into it (see module docstrings).
    """

    external_id: str
    url: str
    cleaned_text: str
    title: str | None = None
    published_at: datetime | None = None
    categories: list[str] = field(default_factory=list)
    license_flag: str = "summarize_only"
    scrape_allowed: bool = True


async def upsert_items(source_class: str, items: list[SourceItem]) -> int:
    """Upsert ``items`` into ``sources`` on ``(source_class, external_id)``.

    Returns the number of rows processed. Mirrors ``arxiv.upsert_papers``: a
    single bulk ``INSERT ... ON CONFLICT DO UPDATE`` so re-ingesting refreshes
    text/title/timestamps in place rather than duplicating rows.
    """
    if not items:
        return 0

    now = datetime.now(timezone.utc)
    rows = [
        {
            "source_class": source_class,
            "external_id": it.external_id,
            "url": it.url,
            "title": it.title,
            "categories": it.categories,
            "cleaned_text": it.cleaned_text,
            "published_at": it.published_at,
            "retrieved_at": now,
            "license_flag": it.license_flag,
            "scrape_allowed": it.scrape_allowed,
            "url_hash": sha256_hex(it.url),
        }
        for it in items
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
