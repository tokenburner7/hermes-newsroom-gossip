"""Draft stage: Research-Synthesis envelope via DeepSeek JSON mode (plan §4 Day 4).

Drafting runs **only after** ``record_claims`` has locked provenance (plan §9).
It feeds the locked claims + analyst notes to DeepSeek with
``response_format={"type": "json_object"}`` and validates the reply against
:class:`ArticleEnvelope` (the Research-Synthesis template, vertical amendment V3).
On a validation failure the model is shown the error and asked to fix it, up to
``max_retries`` times.

Editorial invariants enforced by the prompt: numbers are immutable (V4), every
technical claim is paired with a concrete crypto implication, and no fact may be
introduced that is not in the locked claim set.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from ..config import settings
from ..db import get_sync_session_factory
from ..llm import get_client
from ..models import Article, Claim, Run
from ..telemetry import traced
from .research import ResearchResult

log = logging.getLogger(__name__)

# Phase-0 disclosure label (O-C1): auto-gated content never claims human review.
LABEL_AUTO_GATED = "AI-generated · automated quality-gated"

# --- Editorial voice & style guide (Phase 1 — format discipline) ---------

STYLE_GUIDE = """\
VOICE: Write like a text from a chronically-online, well-connected friend.
Snarky, fast, name-dropping, insider-y. The reader feels like they're getting a
DM from someone who hears everything first. Think: group chat, not analyst
report. Think: @DeuxMoi energy — but with provenance. The attitude is in the
delivery; the facts stay sourced.

STRUCTURE (every article):
1. LEDE — The news. WHO did WHAT. Front-load the name and the action.
   "Selena Gomez and Benny Blanco are engaged, per sources close to the couple."
   NOT "In a surprising turn of events, a major pop star has announced..."
2. THE DETAILS — What we know. How we know it. Sources named. Timeline if relevant.
3. CONTEXT — Why this matters. How this fits the person's arc. Previous related
   stories. "This comes three months after..."
4. THE ANGLE — What's really going on here. Read between the lines. Is this a PR
   move? A damage-control drop? A genuine moment?
5. THE TAKE — One sharp sentence. What the reader should walk away thinking.
(These are the beats to hit, not literal section headings — let the prose flow.)

TONAL RULES:
- Names first, actions second: "Timothée Chalamet has signed on to..." NOT
  "A new project has attracted..."
- Drop the journalist distance: "Per sources close to the production...",
  "Insiders tell us...", "A rep for [Name] confirmed..."
- Speed over formality. Short paragraphs, 1-3 sentences. White space is your friend.
- Specific over general: "$4.2M per episode" not "a lucrative deal"
- No clickbait: never promise more than the article delivers
- No moralizing: report the news, don't judge the choices
- Headlines: punchy, name-forward, ≤80 chars. Good: "Zendaya to Star in
  Guadagnino's New Film". Bad: "Major Casting News: A-List Star Joins Upcoming Project"
- Banned openers: "In a shocking turn of events...", "Fans are losing it over...",
  "The internet is buzzing about...", "In these uncertain times...", "As we all know..."
- Citations: every non-obvious fact gets a [^claim_N] marker
- Quotes: use exact words when available. "I'm taking a break from acting" hits
  harder than a paraphrase.
- No hedging: "may possibly be considering" → kill it. If it's unconfirmed, say
  "has not been confirmed" and move on.
- End with a kick: the last line should resonate — a punchline, a question, a what's-next.
"""

# Voice violations detected by regex (no LLM call — cheap, fast).
_VOICE_CHECKS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"\b(revolutionary|game-changing|groundbreaking|unprecedented)\b", re.I
        ),
        "banned hype word",
    ),
    (
        re.compile(
            r"(?:may|might|could|potentially|possibly|suggest[s]?)\b"
            r".*?\b(?:may|might|could|potentially|possibly)\b"
            r".*?\b(?:may|might|could|potentially|possibly)\b",
            re.I | re.DOTALL,
        ),
        "hedging chain (3+ hedges in close succession)",
    ),
]


def check_voice(body: str) -> list[str]:
    """Return voice/style violations found in *body*.  Empty list = clean."""
    violations: list[str] = []
    for pattern, msg in _VOICE_CHECKS:
        matches = pattern.findall(body)
        if matches:
            item = str(matches[0])[:60] if matches else ""
            violations.append(f"{msg}: {item}")
    return violations


# Citation marker in body text: [^claim_<id>]
_CITE_RE = re.compile(r"\[\^claim_(\d+)\]")


def _find_cited_ids(body: str) -> set[int]:
    """Return the set of claim ids already cited in *body*."""
    return {int(m) for m in _CITE_RE.findall(body)}


def _insert_citations(body: str, claims: list[Claim]) -> str:
    """Insert [^claim_N] markers into *body* for claims whose text appears.

    Performs simple substring matching: for each claim whose id is not already
    cited, finds the claim_text (or first sentence of it) in the body and
    inserts the citation marker after the match. Inserts descending by
    position so offsets stay valid.
    """
    cited = _find_cited_ids(body)
    missing = [c for c in claims if c.id not in cited]
    if not missing:
        return body

    insertions: list[tuple[int, str]] = []
    for claim in missing:
        needle = claim.claim_text.strip()
        if len(needle) > 120:
            first_sent = re.split(r"(?<=[.!?])\s+", needle)[0]
            needle = first_sent if len(first_sent) > 20 else needle[:120]

        pos = body.find(needle)
        if pos == -1:
            needle_short = needle[:60]
            pos = body.find(needle_short)

        if pos != -1:
            insert_at = pos + len(needle)
            marker = f"[^claim_{claim.id}]"
            insertions.append((insert_at, marker))

    if not insertions:
        return body

    insertions.sort(key=lambda x: x[0], reverse=True)
    result = body
    for ins_pos, marker in insertions:
        result = result[:ins_pos] + marker + result[ins_pos:]
    return result


class ArticleEnvelope(BaseModel):
    """Validated Research-Synthesis article (vertical amendment V3)."""

    model_config = ConfigDict(extra="ignore")

    type: str = "breaking_sighting"
    headline: str = Field(min_length=8, max_length=200)
    dek: str = ""
    key_claims: list[str] = Field(min_length=1)
    body: str = Field(min_length=200)
    # Each entry pairs a technical finding with a concrete crypto implication
    # ("<finding> -> <implication>") — the synthesis-not-summary product rule.
    implications: list[str] = Field(min_length=1)
    market_context: str = ""
    related_papers: list[str] = Field(default_factory=list)
    claims_used: list[int] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)

    # --- Type-specific optional fields (populated per article_type) -------
    # regulatory_signal
    filing_type: str = ""
    affected_markets: list[str] = Field(default_factory=list)
    key_provisions: list[str] = Field(default_factory=list)
    polymarket_context: str = ""
    # market_context
    market_snapshot: dict = Field(default_factory=dict)
    macro_context: str = ""
    # infrastructure_spotlight
    crypto_use_cases: list[str] = Field(default_factory=list)
    # prediction_market_signal
    probability_trajectory: dict = Field(default_factory=dict)
    # weekly_deep_dive
    sections: list = Field(default_factory=list)
    key_takeaways: list[str] = Field(default_factory=list)
    data_appendix: str = ""


# Type-specific JSON keys appended to the base draft prompt per article type.
_TYPE_KEY_HINTS: dict[str, str] = {
    "breaking_sighting": (
        '  "who": "the celebrity / celebrities spotted",\n'
        '  "where": "venue, city, neighborhood — be specific",\n'
        '  "when": "date / time of the sighting",\n'
        '  "with_whom": ["who they were with, or empty"],\n'
        '  "what_wearing": "outfit details if noted, or empty string",\n'
        '  "significance": "why this sighting matters",\n'
    ),
    "feud_coverage": (
        '  "parties": ["who is feuding"],\n'
        '  "inciting_incident": "what kicked it off",\n'
        '  "timeline": ["dated beats of the back-and-forth"],\n'
        '  "current_status": "where things stand now",\n'
        '  "power_dynamic": "who has the upper hand and why",\n'
    ),
    "casting_news": (
        '  "actor": "who is cast",\n'
        '  "project": "title of the film / show",\n'
        '  "studio": "studio / network / streamer",\n'
        '  "role": "the character or role",\n'
        '  "status": "e.g. in talks, signed, confirmed",\n'
        '  "previous_project_context": "their last notable role / relevant history",\n'
    ),
    "box_office_report": (
        '  "film": "the title",\n'
        '  "gross": "the figure with window (e.g. $X opening weekend)",\n'
        '  "budget": "reported production budget, or empty string",\n'
        '  "projection": "where it is tracking next, or empty string",\n'
        '  "records": ["records broken or approached"],\n'
        '  "ranking_context": "how it ranks vs comparable releases",\n'
    ),
    "blind_item": (
        '  "clues": ["the breadcrumbs given, verbatim where possible"],\n'
        '  "possible_subjects": ["who the clues point to, if named in claims"],\n'
        '  "source_credibility": "how solid the tip is",\n'
        '  "implied_story": "what is being hinted without saying it outright",\n'
    ),
    "relationship_update": (
        '  "parties": ["the people involved"],\n'
        '  "status_change": "e.g. engaged, split, dating, expecting",\n'
        '  "timeline": ["how the relationship got here"],\n'
        '  "confirmation_source": "who confirmed (rep, the couple, sources)",\n'
        '  "third_parties": ["exes / others relevant to the story, or empty"],\n'
    ),
    "album_drop": (
        '  "artist": "who is releasing",\n'
        '  "album": "title of the album / project",\n'
        '  "release_date": "the drop date",\n'
        '  "projections": "first-week / chart projections, or empty string",\n'
        '  "label": "label / distributor",\n'
        '  "lead_single": "the lead single, or empty string",\n'
    ),
    "fashion_moment": (
        '  "celebrity": "who wore it",\n'
        '  "designer": "designer / house",\n'
        '  "event": "red carpet / event / occasion",\n'
        '  "pieces": ["the standout pieces / looks"],\n'
        '  "price_point": "reported cost, if given, or empty string",\n'
        '  "significance": "why the look is a moment",\n'
    ),
    "career_milestone": (
        '  "person": "whose milestone",\n'
        '  "achievement": "the award / record / first",\n'
        '  "context": "what makes it notable",\n'
        '  "trajectory_impact": "what it means for their career arc",\n'
        '  "competition": ["who else was in the running, or empty"],\n'
    ),
    "viral_moment": (
        '  "what_happened": "the moment, plainly",\n'
        '  "platform": "where it blew up (TikTok, X, etc.)",\n'
        '  "metrics": "views / likes / shares as given",\n'
        '  "participants": ["who is involved"],\n'
        '  "lifecycle_stage": "e.g. peaking, cresting, already a meme",\n'
    ),
}

# Required (present and non-empty) type-specific fields per article type, checked
# after schema validation in draft() so each variant carries its own payload.
TYPE_FIELDS: dict[str, list[str]] = {
    # Gossip types are narrative — who/where/when live in the body prose,
    # not as structured JSON fields. No required type-specific keys.
    "breaking_sighting": [],
    "feud_coverage": [],
    "casting_news": [],
    "box_office_report": [],
    "blind_item": [],
    "relationship_update": [],
    "album_drop": [],
    "fashion_moment": [],
    "career_milestone": [],
    "viral_moment": [],
}


def _build_system_prompt(article_type: str, *, style_guide: str | None = None) -> str:
    """Build the draft system prompt for ``article_type`` (base + type-specific keys).

    If ``style_guide`` is provided, it replaces the module-level STYLE_GUIDE (used for
    vertical-specific voice/audience — e.g. finance vs. crypto-native).
    """
    guide = style_guide if style_guide is not None else STYLE_GUIDE
    extra = _TYPE_KEY_HINTS.get(article_type, "")
    extra_block = (",\n  // type-specific fields:\n" + extra) if extra else "\n"
    return f"""\
You are the writer for an autonomous celebrity gossip newsroom.
{guide}

Write ONE "{article_type}" article from a LOCKED evidence base of claims. You are given the claims (each with a verbatim supporting span and a source URL) and the reporter's notes.

Hard rules:
- Use ONLY the provided claims as factual basis. Introduce NO new facts, names, numbers, or sources. Numbers are IMMUTABLE: reproduce every figure ($, dates, ages, grosses, deal terms) exactly as given.
- Lead with the name and the action. Front-load WHO did WHAT \u2014 the name and the verb come first.
- Pair the facts with the angle: for each main beat, say what it really means — the PR move, the damage-control drop, the arc it fits. That read-between-the-lines is what gossip readers come for, and it is what the "implications" key carries here.
- If the tip is thin, write a shorter, sharper item \u2014 never pad with speculation or filler to reach a word count. A tight 250 words beats a padded 500.
- No hedging. If something is unconfirmed, say "has not been confirmed" and move on \u2014 never "may possibly be considering."
- Never contradict a supporting span. Use the exact quoted words when a claim gives them to you.
- Tag EVERY non-obvious fact in the body with its claim-id footnote marker. Use the format [^claim_N] where N is the claim_id shown in the locked claims. Place the marker immediately after the sentence or clause that uses that claim.
- Honour the STRUCTURE and TONAL RULES in the style guide above.

Output a SINGLE JSON object (no markdown fences, no prose around it) with these keys:
{{
  "type": "{article_type}",
  "headline": "punchy, name-forward, <= 80 chars, no clickbait",
  "dek": "1-2 sentence standfirst — the hook",
  "key_claims": ["concise restatement of each key fact", "..."],
  "body": "the article in Markdown, ~250-500 words, grounded entirely in the claims, with [^claim_N] footnote markers",
  "implications": ["<fact> -> <what it really means / the angle>", "..."],
  "market_context": "1-3 sentences on how this fits the person's arc / the wider story, or an empty string",
  "related_papers": ["titles or URLs of related stories / prior coverage named in the sources"],
  "claims_used": [<claim_id integers you actually used>],
  "suggested_tags": ["lowercase-hyphenated-tags"]{extra_block}}}
Return valid JSON only."""


def _missing_type_fields(envelope: "ArticleEnvelope", article_type: str) -> list[str]:
    """Return required type-specific fields that are missing or empty."""
    missing: list[str] = []
    for name in TYPE_FIELDS.get(article_type, []):
        value = getattr(envelope, name, None)
        if value is None or (isinstance(value, (str, list, dict)) and len(value) == 0):
            missing.append(name)
    return missing


def _slugify(text: str, *, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "article"


def _load_claims(run_id: int, claim_ids: list[int]) -> list[Claim]:
    factory = get_sync_session_factory()
    with factory() as session:
        stmt = select(Claim)
        if claim_ids:
            stmt = stmt.where(Claim.id.in_(claim_ids))
        else:
            stmt = stmt.where(Claim.run_id == run_id)
        return list(session.execute(stmt.order_by(Claim.id)).scalars().all())


def _claims_block(claims: list[Claim]) -> str:
    out: list[str] = []
    for c in claims:
        span = (c.supporting_span or "").replace("\n", " ")
        out.append(
            f"[claim_id={c.id}] CLAIM: {c.claim_text}\n"
            f"  span: \"{span}\"\n"
            f"  source: {c.source_url}"
        )
    return "\n".join(out)


def _build_user_message(notes: str, claims: list[Claim]) -> str:
    return (
        f"Reporter's notes:\n{notes or '(none provided)'}\n\n"
        "Locked claims (immutable evidence base — use ONLY these):\n"
        f"{_claims_block(claims)}\n\n"
        "Write the gossip article now as a single JSON object."
    )


@traced("draft")
def draft(
    run_id: int,
    research: ResearchResult,
    *,
    article_type: str = "research_synthesis",
    model: str | None = None,
    max_retries: int = 2,
    style_guide: str | None = None,
) -> ArticleEnvelope:
    """Draft the article envelope for ``run_id`` as ``article_type``.

    Validates the model's JSON against :class:`ArticleEnvelope`, retrying with the
    validation error fed back (≤ ``max_retries``). ``claims_used`` on the returned
    envelope is forced to the locked claim ids so provenance can't drift. Raises
    :class:`ValueError` if drafting never produces a valid envelope.

    Pass ``style_guide`` to override the module-level STYLE_GUIDE for a different
    content vertical (e.g. finance vs. crypto-native voice).
    """
    claims = _load_claims(run_id, research.claim_ids)
    if not claims:
        raise ValueError(f"draft(run_id={run_id}): no locked claims to draft from")

    model = model or settings.model_primary
    client = get_client()

    messages: list[dict] = [
        {"role": "system", "content": _build_system_prompt(article_type, style_guide=style_guide)},
        {"role": "user", "content": _build_user_message(research.research_notes, claims)},
    ]

    locked_ids = [c.id for c in claims]
    last_err: str | None = None

    for attempt in range(max_retries + 1):
        result = client.chat(
            messages,
            model=model,
            response_format={"type": "json_object"},
            max_tokens=2400,
            temperature=0.4,
        )
        raw = result.text.strip()
        try:
            data = json.loads(raw)
            envelope = ArticleEnvelope.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_err = str(exc)
            log.warning("draft validation failed (attempt %d): %s", attempt + 1, last_err)
            if attempt < max_retries:
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "That was not a valid article object. Fix this error and "
                            f"return ONLY the corrected JSON object:\n{last_err}"
                        ),
                    },
                ]
            continue

        # Each article type must carry its own required payload (V3 variants).
        missing = _missing_type_fields(envelope, article_type)
        if missing:
            last_err = (
                f"missing/empty required fields for {article_type}: "
                + ", ".join(missing)
            )
            log.warning("draft type-field check failed (attempt %d): %s", attempt + 1, last_err)
            if attempt < max_retries:
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            f"The JSON is missing required fields for a {article_type} "
                            f"article: {', '.join(missing)}. Add them (non-empty) and "
                            "return ONLY the corrected JSON object."
                        ),
                    },
                ]
            continue

        # Lock provenance: claims_used always reflects the recorded claim ids,
        # and type is stamped to the requested article_type.
        envelope.claims_used = locked_ids
        envelope.type = article_type

        # Insert citation markers for any claims not yet cited (post-hoc safety net).
        envelope.body = _insert_citations(envelope.body, claims)

        # Voice/style check (cheap regex pass — no extra LLM call).
        voice_issues = check_voice(envelope.body)
        if voice_issues and attempt < max_retries:
            last_err = "; ".join(voice_issues)
            log.info("draft voice issues (attempt %d): %s", attempt + 1, last_err)
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "The draft has voice/style violations:\n- "
                        + "\n- ".join(voice_issues)
                        + "\n\nRewrite the article fixing these while keeping every "
                        "[^claim_N] citation and all figures exactly as locked. "
                        "Return ONLY the corrected JSON object."
                    ),
                },
            ]
            continue

        _set_stage(run_id, "drafted")
        log.info("draft run=%d ok (%d claims, headline=%r)", run_id, len(locked_ids), envelope.headline)
        return envelope

    raise ValueError(f"draft(run_id={run_id}) failed after {max_retries + 1} attempts: {last_err}")


def _set_stage(run_id: int, stage: str) -> None:
    factory = get_sync_session_factory()
    with factory() as session:
        run = session.get(Run, run_id)
        if run is not None:
            run.stage = stage
            session.commit()


def persist_article(
    run_id: int,
    envelope: ArticleEnvelope,
    *,
    slug_suffix: str | int | None = None,
    fact_pass_rate: float | None = None,
    quality_score: float | None = None,
    body_final_md: str | None = None,
    status: str = "drafted",
    vertical: str = "gossip",
) -> int:
    """Insert an ``articles`` row from a validated envelope; return its id.

    The slug is derived from the headline with ``slug_suffix`` (default: the
    ``run_id``) appended for uniqueness. ``review_path`` is Phase-0 ``auto_gated``
    and ``label`` is derived from it (O-C1 — honest disclosure from the first row).

    ``quality_score`` is the gate judge's weighted score (Phase 1 Week 2) and
    ``body_final_md`` is the verified humanized body, when present — it is what the
    publisher renders, leaving the raw ``body_md`` draft intact for provenance.

    ``vertical`` tags the article to a content vertical (default "aixcrypto").
    """
    suffix = run_id if slug_suffix is None else slug_suffix
    slug = f"{_slugify(envelope.headline)}-{suffix}"

    factory = get_sync_session_factory()
    with factory() as session:
        article = Article(
            run_id=run_id,
            slug=slug,
            type=envelope.type,
            vertical=vertical,
            headline=envelope.headline,
            dek=envelope.dek or None,
            body_md=envelope.body,
            body_final_md=body_final_md,
            envelope_json=envelope.model_dump(),
            claims_used=envelope.claims_used or None,
            fact_pass_rate=fact_pass_rate,
            quality_score=quality_score,
            review_path="auto_gated",
            label=LABEL_AUTO_GATED,
            status=status,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(article)
        session.commit()
        article_id = article.id
    log.info("persisted article id=%d slug=%s status=%s", article_id, slug, status)
    return article_id
