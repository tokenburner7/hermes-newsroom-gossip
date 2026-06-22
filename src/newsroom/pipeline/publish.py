"""Publish stage: render a fact-gated article to the Astro content collection (plan §4 Day 6).

:func:`publish` reads the persisted ``articles`` row for a run and writes a
Markdown file into ``web/src/content/articles/<slug>.md``. The YAML frontmatter
mirrors the Astro Zod schema (``web/src/content.config.ts``) field-for-field and
the article body becomes the Markdown content, so Astro can both type-check the
metadata and render the prose via ``<Content/>``.

Honesty / gating invariants (plan §9):
* Only an article that *passed* the fact gate (status ``fact_checked``) — or one
  already ``published`` — may be published. A ``drafted`` (gate-failed) article is
  refused; we never silently lower the gate.
* The disclosure ``label`` and ``review_path`` are carried through verbatim (O-C1).

Idempotency: if the row is already ``published`` the file is not rewritten and a
result with status ``already_published`` is returned.

:func:`republish` (Phase 3) re-renders an existing article *unconditionally* — it
is the substrate for the corrections/retraction workflow and the SEO-cluster
cross-link / noindex updates, all of which must rewrite a live file and bump
``dateModified`` without going through the publish gate again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from ..db import get_sync_session_factory
from ..models import Article, Claim, Source
from .draft import ArticleEnvelope
from ..telemetry import traced

log = logging.getLogger(__name__)

# --- SEO constants -----------------------------------------------------------
META_TITLE_MAX = 70
META_DESCRIPTION_MAX = 155
SLUG_MAX_WORDS = 6
_ELLIPSIS = "…"


def make_slug(headline: str, source_id: int) -> str:
    """Kebab-case slug from headline, ≤6 words, with source_id suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", headline.lower()).strip("-")
    words = slug.split("-")[:SLUG_MAX_WORDS]
    return "-".join(words) + f"-{source_id}"


def seo_title(headline: str) -> str:
    """Truncate headline to ≤70 chars for meta title tag."""
    return f"{headline[:META_TITLE_MAX - 1]}{_ELLIPSIS}" if len(headline) > META_TITLE_MAX else headline


def seo_description(dek: str) -> str:
    """Truncate dek to ≤155 chars for meta description."""
    return f"{dek[:META_DESCRIPTION_MAX - 1]}{_ELLIPSIS}" if len(dek) > META_DESCRIPTION_MAX else dek


def _iso(value) -> str:
    """ISO-8601 string for a date/datetime, tolerating plain strings."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def schema_jsonld(
    article_type: str,
    headline: str,
    dek: str,
    published_at,
    tags: list[str],
    *,
    date_modified=None,
    corrections: list[str] | None = None,
    retraction: dict | None = None,
) -> dict:
    """Generate schema.org Article JSON-LD.

    ``date_modified`` (Phase 3) is emitted as ``dateModified`` so search engines
    see edits. ``corrections`` become ``CorrectionComment`` entries on the
    ``correction`` property; a ``retraction`` adds a final ``CorrectionComment``
    flagging the article as retracted (schema.org has no dedicated retraction
    type, so a clearly-labelled correction is the honest representation).
    """
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": headline[:110],
        "description": seo_description(dek),
        "datePublished": _iso(published_at),
        "dateModified": _iso(date_modified or published_at),
        "author": {"@type": "Organization", "name": "The Gossip"},
        "isAccessibleForFree": True,
        "genre": article_type.replace("_", " ").title(),
        "about": tags[:5] if tags else [],
    }

    comments: list[dict] = [
        {"@type": "CorrectionComment", "text": note}
        for note in (corrections or [])
    ]
    if retraction:
        comments.append(
            {
                "@type": "CorrectionComment",
                "text": f"RETRACTED: {retraction.get('reason', '')}".strip(),
                "datePublished": _iso(retraction.get("date") or date_modified or published_at),
            }
        )
    if comments:
        data["correction"] = comments
    return data


# Repo root: .../src/newsroom/pipeline/publish.py -> parents[3] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: Where published Markdown lands; consumed by the Astro `articles` collection.
CONTENT_DIR = _REPO_ROOT / "web" / "src" / "content" / "articles"

#: Statuses from which publishing is allowed (the fact gate must have passed).
PUBLISHABLE_STATUSES = frozenset({"fact_checked", "published"})

#: Marker delimiting auto-generated SEO cross-link blocks in body_final_md so they
#: can be regenerated idempotently (see :mod:`newsroom.seo_clusters`).
CROSSLINK_BEGIN = "<!-- seo-cross-links:begin -->"
CROSSLINK_END = "<!-- seo-cross-links:end -->"


@dataclass(slots=True)
class PublishResult:
    """Outcome of :func:`publish` / :func:`republish`."""

    run_id: int | None
    article_id: int
    slug: str
    file_path: str
    status: str  # 'published' | 'already_published' | 'republished'

    @property
    def already_published(self) -> bool:
        return self.status == "already_published"


# Preserve YAML block-scalar formatting (`|`) for the multi-line Markdown body so
# the frontmatter stays readable instead of one giant escaped string.
def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _FrontmatterDumper(yaml.SafeDumper):
    pass


_FrontmatterDumper.add_representer(str, _str_representer)


def _load_article(session, run_id: int) -> Article | None:
    """Return the (newest) ``articles`` row for ``run_id``, or ``None``."""
    return session.execute(
        select(Article)
        .where(Article.run_id == run_id)
        .order_by(Article.id.desc())
    ).scalars().first()


def _build_sources(session, article: Article) -> list[dict[str, str]]:
    """Distinct ``{url, title}`` sources behind the article's locked claims.

    Prefers the claims the article actually used (``claims_used``); falls back to
    every claim on the run. Titles are resolved from the ``sources`` table.
    """
    claim_ids = article.claims_used or []
    stmt = select(Claim)
    stmt = stmt.where(Claim.id.in_(claim_ids)) if claim_ids else stmt.where(
        Claim.run_id == article.run_id
    )
    claims = session.execute(stmt.order_by(Claim.id)).scalars().all()

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for claim in claims:
        url = claim.source_url
        if not url or url in seen:
            continue
        seen.add(url)
        title: str | None = None
        if claim.source_id is not None:
            src = session.get(Source, claim.source_id)
            title = src.title if src else None
        if not title:
            src = session.execute(
                select(Source).where(Source.url == url)
            ).scalar_one_or_none()
            title = src.title if src else None
        out.append({"url": url, "title": title or url})
    return out


def _build_claim_evidence(session, article: Article) -> list[dict]:
    """Build ``claim_evidence`` list for frontmatter: one entry per locked claim.

    Each entry carries the claim text, its verbatim supporting span, the SHA-256
    hash of that span, and the resolved source URL + title.  This is the data
    that powers the reader-facing Evidence & Provenance panel on the article page.
    """
    claim_ids = article.claims_used or []
    if not claim_ids:
        return []

    claims = session.execute(
        select(Claim).where(Claim.id.in_(claim_ids)).order_by(Claim.id)
    ).scalars().all()

    evidence: list[dict] = []
    for c in claims:
        title: str | None = None
        if c.source_id is not None:
            src = session.get(Source, c.source_id)
            title = src.title if src else None
        evidence.append({
            "claim_id": c.id,
            "claim_text": c.claim_text,
            "supporting_span": c.supporting_span,
            "span_sha256": c.span_sha256,
            "source_url": c.source_url,
            "source_title": title or c.source_url,
        })
    return evidence


def _retraction_payload(article: Article) -> dict | None:
    """Structured retraction info for frontmatter / schema.org, or ``None``."""
    if article.status != "retracted" and not article.retracted_at:
        return None
    return {
        "reason": article.retraction_reason or "",
        "date": _iso(article.retracted_at) if article.retracted_at else "",
    }


def _render_body_with_notices(body: str, article: Article) -> str:
    """Prepend retraction / correction notices to the rendered Markdown body.

    These are *render-time* injections: the stored ``body_md`` / ``body_final_md``
    stay clean for provenance. SEO cross-links, by contrast, are persisted into
    ``body_final_md`` and so are already present in ``body`` here.
    """
    notices: list[str] = []
    if article.status == "retracted":
        reason = article.retraction_reason or "No reason given."
        notices.append(
            f"> **⛔ RETRACTED.** This article has been retracted and should no "
            f"longer be relied upon. Reason: {reason}"
        )
    for note in article.correction_notes or []:
        notices.append(f"> **✏️ Correction.** {note}")

    if not notices:
        return body
    return "\n\n".join(notices) + "\n\n" + body


def _build_frontmatter(session, article: Article, envelope: ArticleEnvelope, *, now: datetime) -> dict:
    """Assemble the YAML frontmatter dict for one article row.

    Shared by :func:`publish` and :func:`republish` (DRY). ``date_modified`` is
    ``now`` for a (re)publish; ``published_at`` is preserved across republishes.
    """
    published_at = (article.published_at or now).date()
    date_modified = now.date()
    raw_body = article.body_final_md or article.body_md or envelope.body
    body = _render_body_with_notices(raw_body, article)
    retraction = _retraction_payload(article)
    corrections = list(article.correction_notes or [])

    # Order mirrors the Astro Zod schema in web/src/content.config.ts.
    return {
        "type": article.type,
        "headline": article.headline,
        "dek": article.dek or envelope.dek or "",
        "published_at": published_at,
        "date_modified": date_modified,
        "body_md": body,
        "implications": list(envelope.implications),
        "sources": _build_sources(session, article),
        "claim_evidence": _build_claim_evidence(session, article),
        "review_path": article.review_path,
        "label": article.label,
        "status": article.status,
        "tags": list(envelope.suggested_tags),
        # SEO metadata (Phase 1 Week 3 + Phase 3 clusters)
        "seo_title": seo_title(article.headline),
        "seo_description": seo_description(article.dek or envelope.dek or ""),
        "noindex": bool(article.noindex),
        "canonical_url": article.canonical_url or "",
        # Corrections / retraction (Phase 3)
        "correction_count": int(article.correction_count or 0),
        "corrections": corrections,
        "retraction": retraction,
        "schema_jsonld": schema_jsonld(
            article.type,
            article.headline,
            article.dek or "",
            published_at,
            list(envelope.suggested_tags),
            date_modified=date_modified,
            corrections=corrections,
            retraction=retraction,
        ),
    }, body


def _render_markdown(frontmatter: dict, body: str) -> str:
    """Serialize ``frontmatter`` as YAML and prepend it to the Markdown ``body``."""
    fm = yaml.dump(
        frontmatter,
        Dumper=_FrontmatterDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return f"---\n{fm}---\n\n{body.strip()}\n"


def _write_article_file(slug: str, frontmatter: dict, body: str) -> Path:
    """Render + write ``<slug>.md`` into the content collection; return the path."""
    markdown = _render_markdown(frontmatter, body)
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    file_path = CONTENT_DIR / f"{slug}.md"
    file_path.write_text(markdown, encoding="utf-8")
    return file_path


@traced("publish")
def publish(run_id: int, env: ArticleEnvelope | None = None) -> PublishResult:
    """Publish the article for ``run_id`` into the Astro content collection.

    ``env`` is optional: when chaining from ``run-once`` the freshly-drafted
    envelope is passed; the standalone ``newsroom publish`` command omits it and
    the envelope is reconstructed from the persisted ``envelope_json``.

    Raises :class:`ValueError` if there is no article for the run or its status is
    not publishable (i.e. the fact gate has not passed).
    """
    now = datetime.now(timezone.utc)
    factory = get_sync_session_factory()
    with factory() as session:
        article = _load_article(session, run_id)
        if article is None:
            raise ValueError(f"publish(run_id={run_id}): no article found for run")

        if article.status == "published":
            file_path = CONTENT_DIR / f"{article.slug}.md"
            log.info("publish run=%d: already published (slug=%s)", run_id, article.slug)
            return PublishResult(
                run_id=run_id,
                article_id=article.id,
                slug=article.slug,
                file_path=str(file_path),
                status="already_published",
            )

        if article.status not in PUBLISHABLE_STATUSES:
            raise ValueError(
                f"publish(run_id={run_id}): article status {article.status!r} is not "
                "publishable — the fact gate has not passed (plan §9: never lower the gate)"
            )

        envelope = env or ArticleEnvelope.model_validate(article.envelope_json)
        frontmatter, body = _build_frontmatter(session, article, envelope, now=now)
        file_path = _write_article_file(article.slug, frontmatter, body)

        article.status = "published"
        article.published_at = now
        article.updated_at = now
        session.commit()

        log.info(
            "published run=%d article=%d slug=%s -> %s",
            run_id, article.id, article.slug, file_path,
        )
        return PublishResult(
            run_id=run_id,
            article_id=article.id,
            slug=article.slug,
            file_path=str(file_path),
            status="published",
        )


def republish(article_id: int) -> PublishResult:
    """Re-render an existing article's file unconditionally and bump dateModified.

    Used by the corrections/retraction workflow and the SEO-cluster updates: it
    rewrites ``<slug>.md`` from the current DB row (including retraction /
    correction notices, noindex, canonical and any cross-links already persisted
    into ``body_final_md``) and stamps ``updated_at`` = now. It does *not* run the
    publish gate — the article was already gated when first published.
    """
    now = datetime.now(timezone.utc)
    factory = get_sync_session_factory()
    with factory() as session:
        article = session.get(Article, article_id)
        if article is None:
            raise ValueError(f"republish(article_id={article_id}): no such article")
        envelope = ArticleEnvelope.model_validate(article.envelope_json)
        frontmatter, body = _build_frontmatter(session, article, envelope, now=now)
        file_path = _write_article_file(article.slug, frontmatter, body)

        article.updated_at = now
        session.commit()

        log.info("republished article=%d slug=%s -> %s", article_id, article.slug, file_path)
        return PublishResult(
            run_id=article.run_id,
            article_id=article.id,
            slug=article.slug,
            file_path=str(file_path),
            status="republished",
        )
