"""Load a published article, repackage it per channel, and persist the payloads."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import settings
from ..db import get_sync_session_factory
from ..llm import LLMError, get_client
from ..models import Article, Claim, Distribution
from .prompts import (
    TELEGRAM_SYSTEM,
    TWEET_SOFT_MAX,
    X_THREAD_SYSTEM,
    build_telegram_user,
    build_x_thread_user,
)

log = logging.getLogger(__name__)

X_THREAD_LEN = 10
TWEET_HARD_MAX = 280
#: Visual delimiter between tweets in the single rendered thread blob (for copy/paste).
THREAD_SEP = "\n\n———\n\n"


@dataclass(slots=True)
class ArticleContext:
    """The slice of a published article needed to repackage it."""

    article_id: int
    slug: str
    headline: str
    dek: str
    type: str
    body: str
    implications: list[str]
    claims: list[str]
    run_id: int | None = None

    @property
    def url(self) -> str:
        return f"{settings.brand_url.rstrip('/')}/articles/{self.slug}"


@dataclass(slots=True)
class ThreadResult:
    hooks: list[str]
    body_tweets: list[str]
    closing: str
    in_tokens: int = 0
    out_tokens: int = 0

    def assemble(self, hook_index: int = 0) -> list[str]:
        """Full 10-tweet thread for the given hook variant (0=a, 1=b, 2=c)."""
        hook = self.hooks[hook_index] if hook_index < len(self.hooks) else (
            self.hooks[0] if self.hooks else ""
        )
        return [hook, *self.body_tweets, self.closing]

    def render(self, hook_index: int = 0) -> str:
        return THREAD_SEP.join(t.strip() for t in self.assemble(hook_index) if t.strip())

    def overlong(self, hook_index: int = 0) -> list[int]:
        """1-based tweet positions that exceed the hard 280-char cap (should be empty)."""
        return [i + 1 for i, t in enumerate(self.assemble(hook_index)) if len(t) > TWEET_HARD_MAX]

    def payload(self) -> dict:
        return {"hooks": self.hooks, "body_tweets": self.body_tweets, "closing": self.closing}


@dataclass(slots=True)
class TelegramResult:
    bullets: list[str]
    rendered: str
    in_tokens: int = 0
    out_tokens: int = 0

    def payload(self) -> dict:
        return {"bullets": self.bullets, "rendered": self.rendered}


@dataclass(slots=True)
class DistributeResult:
    article_id: int
    url: str
    thread: ThreadResult | None = None
    telegram: TelegramResult | None = None
    distribution_ids: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- load ---------------------------------------------------------------------

def latest_published_article_id() -> int | None:
    """The most recently published article id, or None."""
    factory = get_sync_session_factory()
    with factory() as session:
        return session.execute(
            select(Article.id)
            .where(Article.status == "published")
            .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
        ).scalars().first()


def load_article_context(article_id: int) -> ArticleContext:
    """Load a published article + its locked claims. Raises if missing/not published."""
    factory = get_sync_session_factory()
    with factory() as session:
        article = session.get(Article, article_id)
        if article is None:
            raise LookupError(f"no article with id {article_id}")
        if article.status != "published":
            raise ValueError(
                f"article {article_id} status={article.status!r}; only 'published' "
                "articles can be distributed"
            )
        env = article.envelope_json or {}
        claim_ids = article.claims_used or []
        if claim_ids:
            stmt = select(Claim).where(Claim.id.in_(claim_ids))
        elif article.run_id is not None:
            stmt = select(Claim).where(Claim.run_id == article.run_id)
        else:
            claims: list[str] = []  # no claims_used and no run_id — can't resolve claims
        if claim_ids or article.run_id is not None:
            claims = [c.claim_text for c in session.execute(stmt.order_by(Claim.id)).scalars().all()]
        return ArticleContext(
            article_id=article.id,
            slug=article.slug,
            headline=article.headline,
            dek=article.dek or env.get("dek", ""),
            type=article.type,
            body=article.body_final_md or article.body_md or "",
            implications=list(env.get("implications") or []),
            claims=claims,
            run_id=article.run_id,
        )


# --- idempotency --------------------------------------------------------------

def _already_distributed(article_id: int, channel: str) -> bool:
    """True if this (article, channel) has already been touched.

    Covers clean ``generated``/``posted`` rows *and* a ``partial`` row or any row
    that already has an ``external_url`` (i.e. tweets are live). The latter guards
    against a re-gen blind-reposting a thread that partially posted (see H3).
    """
    from sqlalchemy import or_

    factory = get_sync_session_factory()
    with factory() as session:
        existing = session.execute(
            select(Distribution.id).where(
                Distribution.article_id == article_id,
                Distribution.channel == channel,
                or_(
                    Distribution.status.in_(("generated", "posted", "partial")),
                    Distribution.external_url.isnot(None),
                ),
            )
        ).scalars().first()
        return existing is not None


# --- closing / templating (links never trusted to the LLM) --------------------

def _closing_tweet(ctx: ArticleContext) -> str:
    sub = settings.effective_subscribe_url
    return (
        "Every claim hash-locked to source — SHA-256 verified, not vibes.\n"
        f"Full synthesis: {ctx.url}\n"
        f"Subscribe (3h cadence): {sub}"
    )


def _telegram_render(ctx: ArticleContext, bullets: list[str]) -> str:
    body = "\n".join(f"• {b.strip()}" for b in bullets)
    sub = settings.effective_subscribe_url
    return (
        f"🤖×⛓ {ctx.headline}\n\n"
        f"{body}\n\n"
        f"🔗 Hash-locked sources + full synthesis:\n{ctx.url}\n\n"
        f"Subscribe (3h cadence): {sub}"
    )


def _parse_json(text: str) -> dict:
    """Tolerant JSON parse: strips accidental ```json fences before loading."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:].lstrip() if t.lower().startswith("json") else t
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


# --- generation ---------------------------------------------------------------

def generate_x_thread(ctx: ArticleContext) -> ThreadResult:
    client = get_client()
    if not client.providers:
        raise LLMError("no LLM providers configured for distribution")
    messages = [
        {"role": "system", "content": X_THREAD_SYSTEM},
        {"role": "user", "content": build_x_thread_user(ctx)},
    ]
    res = client.chat(
        messages, model=settings.distribute_model,
        response_format={"type": "json_object"}, max_tokens=1600, temperature=0.7,
    )
    if res.finish_reason == "length":
        raise LLMError(
            "X thread generation truncated (max_tokens hit); bump distribute max_tokens "
            "or shorten the article body"
        )
    data = _parse_json(res.text)
    hooks = [h.strip() for h in (data.get("hooks") or []) if h and h.strip()][:3]
    body = [t.strip() for t in (data.get("body_tweets") or []) if t and t.strip()][:8]
    return ThreadResult(
        hooks=hooks or [ctx.headline], body_tweets=body, closing=_closing_tweet(ctx),
        in_tokens=res.in_tokens, out_tokens=res.out_tokens,
    )


def generate_telegram(ctx: ArticleContext) -> TelegramResult:
    client = get_client()
    if not client.providers:
        raise LLMError("no LLM providers configured for distribution")
    messages = [
        {"role": "system", "content": TELEGRAM_SYSTEM},
        {"role": "user", "content": build_telegram_user(ctx)},
    ]
    res = client.chat(
        messages, model=settings.distribute_model,
        response_format={"type": "json_object"}, max_tokens=600, temperature=0.6,
    )
    if res.finish_reason == "length":
        raise LLMError("Telegram generation truncated (max_tokens hit)")
    data = _parse_json(res.text)
    bullets = [b.strip() for b in (data.get("bullets") or []) if b and b.strip()][:3]
    if not bullets:
        # A contentless post is worse than none: raise so it is retried rather than
        # persisted and then locked in by the idempotency guard (M6).
        raise LLMError("telegram generation produced no usable bullets")
    return TelegramResult(
        bullets=bullets, rendered=_telegram_render(ctx, bullets),
        in_tokens=res.in_tokens, out_tokens=res.out_tokens,
    )


# --- persistence + orchestration ---------------------------------------------

def _meter_spend(run_id: int | None, in_tokens: int, out_tokens: int) -> None:
    """Count distribution LLM spend against the daily budget (reserve + ledger).

    Distribution is an LLM path like any other (O-C3): its cost must reserve against
    and settle into the daily ceiling, not bypass it (H2). Runs sync via ``asyncio.run``
    like the budget calls inside the Temporal activities.
    """
    import asyncio

    from ..budget import estimate_cost_usd, reserve, settle

    cost = estimate_cost_usd(in_tokens, out_tokens, settings.distribute_model)
    if cost <= 0:
        return
    asyncio.run(reserve(est_usd=cost))
    asyncio.run(settle(run_id, cost))


def _persist(article_id: int, channel: str, variant: str, payload: dict,
             rendered: str, in_tokens: int, out_tokens: int) -> int:
    from sqlalchemy.exc import IntegrityError

    factory = get_sync_session_factory()
    with factory() as session:
        row = Distribution(
            article_id=article_id, channel=channel, variant=variant,
            payload_json=payload, rendered_text=rendered, status="generated",
            in_tokens=in_tokens, out_tokens=out_tokens,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Concurrent distributor won the race for this (article, channel) — the
            # partial unique index rejected our insert. Treat it as already-distributed
            # and return the existing row's id (H1).
            session.rollback()
            existing = session.execute(
                select(Distribution.id).where(
                    Distribution.article_id == article_id,
                    Distribution.channel == channel,
                    Distribution.status.in_(("generated", "posted")),
                )
            ).scalars().first()
            if existing is None:
                raise
            return existing
        return row.id


def distribute_article(
    article_id: int,
    channels: tuple[str, ...] = ("x", "telegram"),
    force: bool = False,
) -> DistributeResult:
    """Repackage one published article for the given channels; persist each payload.

    Idempotent: a channel already generated/posted for this article is skipped
    unless ``force`` is set. This makes the 3x/day cron and the Temporal activity
    safe to re-run without piling up duplicate payloads.
    """
    ctx = load_article_context(article_id)
    result = DistributeResult(article_id=article_id, url=ctx.url)

    if "x" in channels:
        if not force and _already_distributed(article_id, "x"):
            result.skipped.append("x")
        else:
            thread = generate_x_thread(ctx)
            result.thread = thread
            _meter_spend(ctx.run_id, thread.in_tokens, thread.out_tokens)
            if len(thread.body_tweets) != 8:
                result.warnings.append(
                    f"x: expected 8 body tweets, got {len(thread.body_tweets)}"
                )
            assembled = thread.assemble(0)
            if len(assembled) != X_THREAD_LEN:
                result.warnings.append(
                    f"x: expected a {X_THREAD_LEN}-tweet thread, got {len(assembled)}"
                )
            over = thread.overlong()
            if over:
                result.warnings.append(f"x: tweets over 280 chars at positions {over}")
            result.distribution_ids["x"] = _persist(
                article_id, "x", "hook_a", thread.payload(), thread.render(0),
                thread.in_tokens, thread.out_tokens,
            )

    if "telegram" in channels:
        if not force and _already_distributed(article_id, "telegram"):
            result.skipped.append("telegram")
        else:
            tg = generate_telegram(ctx)
            result.telegram = tg
            _meter_spend(ctx.run_id, tg.in_tokens, tg.out_tokens)
            if len(tg.bullets) != 3:
                result.warnings.append(f"telegram: expected 3 bullets, got {len(tg.bullets)}")
            result.distribution_ids["telegram"] = _persist(
                article_id, "telegram", "default", tg.payload(), tg.rendered,
                tg.in_tokens, tg.out_tokens,
            )

    log.info(
        "distributed article=%s channels=%s ids=%s skipped=%s",
        article_id, channels, result.distribution_ids, result.skipped,
    )
    return result


def distribute_latest(
    channels: tuple[str, ...] = ("x", "telegram"), force: bool = False
) -> DistributeResult:
    article_id = latest_published_article_id()
    if article_id is None:
        raise LookupError("no published articles to distribute")
    return distribute_article(article_id, channels, force=force)
