"""Corrections & retraction workflow (plan §7, Phase 3).

The ``claims`` table already locks provenance per article (Opus's observation:
it is the substrate a corrections workflow needs). This module adds the two
editorial actions on top of it:

* :func:`retract` — mark an article ``status='retracted'``, record the reason and
  timestamp, and republish it carrying a prominent retraction notice + a bumped
  ``dateModified`` (schema.org gets a ``CorrectionComment`` flagging the retraction).
* :func:`correct` — append a correction note, increment ``correction_count``,
  bump ``dateModified``, and republish. The article *stays* ``published`` (a
  correction does not pull it down — it annotates it).

Both are synchronous (matching the publish stage they reuse) and idempotent in
the sense that they always re-derive the rendered file from the current DB row.
The disclosure label / review_path are never touched here (O-C1 stays honest).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .db import get_sync_session_factory
from .models import Article
from .pipeline import republish

log = logging.getLogger(__name__)

#: Terminal status for a retracted article.
STATUS_RETRACTED = "retracted"


@dataclass(slots=True)
class CorrectionResult:
    """Outcome of :func:`correct` / :func:`retract`."""

    article_id: int
    slug: str
    action: str  # 'corrected' | 'retracted'
    status: str
    correction_count: int
    note: str
    file_path: str


def _get_article(session, article_id: int) -> Article:
    article = session.get(Article, article_id)
    if article is None:
        raise LookupError(f"no article with id {article_id}")
    return article


def _append_note(existing: list[str] | None, note: str) -> list[str]:
    """Return a new list with ``note`` appended (immutable update)."""
    return [*(existing or []), note]


def retract(article_id: int, reason: str) -> CorrectionResult:
    """Retract ``article_id``: set ``status='retracted'`` + republish with notice.

    Records ``retraction_reason`` and ``retracted_at``, appends a dated retraction
    note to ``correction_notes`` (so the audit trail is one place), and bumps
    ``dateModified`` via :func:`newsroom.pipeline.republish`. Raises
    :class:`ValueError` if ``reason`` is empty or :class:`LookupError` if the
    article does not exist.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("retract: a non-empty --reason is required")

    now = datetime.now(timezone.utc)
    factory = get_sync_session_factory()
    with factory() as session:
        article = _get_article(session, article_id)
        if article.status == STATUS_RETRACTED:
            log.info("retract: article %d already retracted (no-op update)", article_id)
        article.status = STATUS_RETRACTED
        article.retraction_reason = reason
        article.retracted_at = now
        article.correction_notes = _append_note(
            article.correction_notes,
            f"[{now.date().isoformat()}] Retracted: {reason}",
        )
        article.updated_at = now
        slug = article.slug
        count = int(article.correction_count or 0)
        session.commit()

    result = republish(article_id)
    log.info("retracted article=%d slug=%s -> %s", article_id, slug, result.file_path)
    return CorrectionResult(
        article_id=article_id,
        slug=slug,
        action="retracted",
        status=STATUS_RETRACTED,
        correction_count=count,
        note=reason,
        file_path=result.file_path,
    )


def correct(article_id: int, note: str) -> CorrectionResult:
    """Apply a correction to ``article_id`` and republish (status stays published).

    Increments ``correction_count``, appends the dated note to
    ``correction_notes``, and bumps ``dateModified``. Raises :class:`ValueError`
    on an empty note, :class:`LookupError` if the article is missing.
    """
    note = (note or "").strip()
    if not note:
        raise ValueError("correct: a non-empty --note is required")

    now = datetime.now(timezone.utc)
    factory = get_sync_session_factory()
    with factory() as session:
        article = _get_article(session, article_id)
        if article.status == STATUS_RETRACTED:
            raise ValueError(
                f"correct: article {article_id} is retracted — cannot correct a "
                "retracted article"
            )
        article.correction_count = int(article.correction_count or 0) + 1
        article.correction_notes = _append_note(
            article.correction_notes,
            f"[{now.date().isoformat()}] {note}",
        )
        article.updated_at = now
        slug = article.slug
        status = article.status
        count = article.correction_count
        session.commit()

    result = republish(article_id)
    log.info(
        "corrected article=%d slug=%s (#%d) -> %s",
        article_id, slug, count, result.file_path,
    )
    return CorrectionResult(
        article_id=article_id,
        slug=slug,
        action="corrected",
        status=status,
        correction_count=count,
        note=note,
        file_path=result.file_path,
    )


def list_corrected(limit: int = 50) -> list[Article]:
    """Return articles that carry corrections or have been retracted."""
    factory = get_sync_session_factory()
    with factory() as session:
        rows = session.execute(
            select(Article)
            .where(
                (Article.correction_count > 0)
                | (Article.status == STATUS_RETRACTED)
            )
            .order_by(Article.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        return list(rows)
