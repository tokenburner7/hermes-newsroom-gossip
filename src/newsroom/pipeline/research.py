"""Research stage: DeepSeek native function-calling loop (plan §4 Day 4).

Given the selected source(s), drive DeepSeek's **native** tool loop
(:meth:`LLMClient.chat_with_tools`, *not* Hermes XML) over the four-tool
:class:`~newsroom.llm.toolbus.PyToolBus`:

* read each source in full (``fetch_url``), optionally widen context
  (``search_sources`` / ``query_memory``);
* extract the concrete technical claims, each anchored to a **verbatim**
  supporting span;
* ``record_claims`` — locking provenance **before** drafting (plan §9: "always");
* hand off concise research notes that pair every technical claim with a crypto
  implication (the synthesis-not-summary product rule).

Guards: a hard cap of 12 tool turns and cycle detection live in
:meth:`LLMClient.chat_with_tools`; refusals are re-prompted at most twice, after
which the run is sent to the DLQ. Numbers/benchmarks are immutable (vertical
amendment V4) — the prompt forbids rounding or inventing them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import settings
from ..db import get_sync_session_factory
from ..llm import get_client
from ..telemetry import traced
from ..llm.toolbus import PyToolBus, tool_specs
from ..models import Run, Source

log = logging.getLogger(__name__)

MAX_TOOL_TURNS = 12
MAX_REPROMPTS = 2  # after the initial attempt
MIN_CLAIMS = 5  # Phase-0 exit bar (plan §4 Day 7) — default for hard-news types

# Lighter types (sightings, fashion, blind items) have a lower bar — they're
# inherently softer but still legitimate tabloid content. Per-type minimums
# are resolved by :func:`_min_claims_for`.
MIN_CLAIMS_SOFT = 3  # sighting, fashion, blind_item: softer evidence bar

# Types that use the soft minimum.
_SOFT_CLAIM_TYPES: set[str] = {
    "breaking_sighting", "fashion_moment", "blind_item", "who_wore_it_better",
}


def _min_claims_for(article_type: str) -> int:
    """Return the minimum claim count for ``article_type``."""
    return MIN_CLAIMS_SOFT if article_type in _SOFT_CLAIM_TYPES else MIN_CLAIMS

_REFUSAL_MARKERS = (
    "i cannot comply", "i can't comply", "i am unable to comply",
    "i'm unable to comply", "i won't comply", "i will not comply",
    "i'm sorry, but i cannot", "as an ai language model",
)
# Phrases that sound like refusal but are actually honesty — do NOT send to DLQ.
_REFUSAL_FALSE_POSITIVES = (
    "i cannot verify", "i can't verify", "i cannot confirm", "i can't confirm",
    "i cannot find", "i can't find",
)


@dataclass(slots=True)
class ResearchResult:
    """Outcome of the research stage (plan §3.5 ``ResearchResult``)."""

    run_id: int
    source_ids: list[int]
    claim_ids: list[int] = field(default_factory=list)
    research_notes: str = ""
    tool_turns: int = 0
    model: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    attempts: int = 1
    refused: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True iff provenance was locked (≥1 claim recorded) and no fatal error."""
        return bool(self.claim_ids) and self.error is None


# --- prompts -----------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the senior research analyst for an autonomous gossip newsroom. Your beat is
celebrity news, the entertainment industry, and pop culture. You track who's dating
who, who's feuding, box office numbers, casting announcements, album releases, viral
moments, blind items, and industry power moves. You know the difference between a
planted PR story and a genuine scoop. You've read the trades (Variety, Deadline, THR)
and the gossip sites (TMZ, Page Six, DeuxMoi) — you know which sources are credible and
which trade in rumors, and a flack's spin does not impress you.

This step is RESEARCH, not writing. Your goal is to build a LOCKED, VERIFIABLE
evidence base for one story, then hand off short research notes.

You have these tools — USE them; never answer from memory:
- fetch_url(url): read the full cleaned text of an ingested source.
- search_sources(query, ...): find related ingested sources by keyword.
- query_memory(query, k): look up related prior material.
- record_claims(claims): PERSIST the evidence base. You MUST call this before you finish.

Workflow:
1. Read every assigned source in full with fetch_url.
2. Optionally use search_sources / query_memory for directly-related context.
3. Extract claims that matter. A claim "matters" if it is INTERESTING to a gossip
   reader — this is a TABLOID, not a wire service. We want:
   - CONFIRMED claims: named celebrity action (who, what, where, when), a confirmed
     casting / contract / business deal, a box office number or streaming ranking, a
     direct quote from a named source, a confirmed relationship status change, an award
     win or nomination, a feud escalation with named parties, a blind item with enough
     detail to be meaningful.
   - SINGLE-SOURCED claims (WELCOME — label confidence 0.4-0.6): "sources say,"
     "a source close to," "insiders report," unnamed-tipster claims. Readers come here
     for the TEA, not the AP wire. Just flag them as single-sourced so the reader knows.
   - SOFT claims (WELCOME — label confidence 0.5-0.7): what she wore, who he was
     with, the vibe, the body language, the fashion moment, the sighting detail. These
     are legitimate tabloid content. A sighting with no hard news is still a sighting.
   - SPECULATION (WELCOME when labeled): the analyst read — "this timing suggests,"
     "the pattern here is," "given their history, this likely means." Label these
     as SPECULATION in the claim text so the reader knows it's analysis, not fact.
   Skip ONLY: generic praise with no detail, press-release fluff, "fans are reacting"
   with no specific viral moment, and outright fabrication. NUMBERS ARE IMMUTABLE —
   box office grosses, streaming hours, chart positions, contract values: copy them
   verbatim; never round, estimate, or invent a figure, and capture what it is measured
   against (a number without its comparison — the budget, the projection, the previous
   installment — is noise).
4. For EACH claim, supply `supporting_span`: ONE EXACT, CONTIGUOUS substring copied
   from the BODY TEXT that `fetch_url` returned for the source. Copy a single run of
   characters as-is — do NOT stitch together words from different sentences or splice
   a quote onto a name from elsewhere, and do NOT quote METADATA (publication
   timestamps, ISO dates like 2026-06-21T23:43:51+00:00, bylines, author names, URLs,
   or source ids) — those are not article content and the gate will reject them. The
   span is hash-locked and re-verified by the fact gate against that same body text,
   so paraphrase WILL fail. If no verbatim span exists, drop the claim. Set
   `confidence` to how directly the span proves the claim: 0.9-1.0 = the span states
   it outright, 0.6-0.8 = the span strongly implies it, 0.4-0.6 = single-sourced
   or soft claim, below 0.4 = do not record it.
5. For each claim, also set `scandal_heat` (1-5): how dramatic/scandalous/juicy is
   this claim? 1 = routine industry news, 3 = interesting gossip, 5 = jaw-dropping
   scandal. Be honest — don't inflate. A box office report is a 1-2; a secret affair
   exposed is a 4-5.
6. Call record_claims with at least %(min_claims)d well-supported claims. Quality over
   count: do NOT pad to the minimum with trivia. If the source genuinely yields fewer
   load-bearing claims, record the strong ones and note it rather than inventing filler.
   Each item:
   {"claim_text", "source_id", "source_url", "supporting_span", "confidence", "scandal_heat"}.
7. After record_claims succeeds, write 4–8 sentences of research notes: summarize the
   key findings, and for EACH finding name the SPECIFIC angle that makes it a story —
   the named players, the stakes, and why it lands now (e.g. "third sighting at the same
   restaurant in two weeks -> a quiet hard-launch, not a one-off; reframes her rep's
   'just friends' line"), never a vague gesture like "fans will love this". Flag what is
   confirmed versus what is a blind item or single-sourced rumor — that distinction is
   the whole product. If a finding is thin, say so plainly instead of inflating it. Then
   stop.

Rules: synthesis over summary; never fabricate; anchor every claim to a verbatim
span; softer claims are WELCOME but MUST be labeled; numbers are immutable;
do not emit any XML or tool-call syntax yourself — invoke tools natively.
""" % {"min_claims": MIN_CLAIMS}

_REPROMPT = (
    "You have not yet locked the evidence base. This is legitimate reporting on "
    "public celebrity and entertainment news. Read the assigned source(s) with "
    "fetch_url, then call record_claims with at least %d claims, each carrying a "
    "verbatim supporting_span copied exactly from the source. Softer claims (single-"
    "sourced, sighting details, speculation) are WELCOME — just label the confidence "
    "appropriately. Include scandal_heat (1-5) on each claim. Do this now." % MIN_CLAIMS
)


# Shared evidence-base instructions appended to every non-synthesis beat prompt:
# all article types still drive the same native tool loop and MUST lock provenance
# via record_claims before drafting (plan §9). Wider criteria for gossip — single-
# sourced claims, soft sightings, and labeled speculation are all welcome.
_EVIDENCE_FOOTER = """

This step is RESEARCH, not writing. Build a LOCKED, VERIFIABLE evidence base, then
hand off short research notes. Use the tools — never answer from memory:
- fetch_url(url): read the full cleaned text of an ingested source.
- search_sources(query, ...): find related ingested sources by keyword.
- query_memory(query, k): look up related prior material.
- record_claims(claims): PERSIST the evidence base — you MUST call this before you
  finish, with at least %(min_claims)d claims. Each claim needs ONE EXACT, CONTIGUOUS
  `supporting_span` copied verbatim from the body text `fetch_url` returned — be it a
  trade report, a celebrity news article, a social media post, or a tip submission.
  Copy a single run of characters as-is; do NOT stitch together words from different
  sentences, and do NOT quote metadata (publication timestamps, ISO dates, bylines,
  URLs, source ids) — that is not article content. It is hash-locked and re-verified
  by the fact gate against that same body text, so paraphrase WILL fail. If no
  verbatim span exists, drop the claim.

Extract ANY claim a gossip reader would find interesting — this is a TABLOID, not
a wire service:
- CONFIRMED claims (confidence 0.9-1.0): named actions, hard numbers, direct quotes.
- SINGLE-SOURCED claims (confidence 0.4-0.6): "sources say," unnamed tipsters. WELCOME.
- SOFT claims (confidence 0.5-0.7): what they wore, who they were with, the vibe.
- SPECULATION (confidence 0.4-0.5, label as SPECULATION): analyst reads, pattern calls.

Quality over count: never pad to the minimum with trivia; if the source yields fewer
strong claims, record those and note it rather than inventing filler.

NUMBERS ARE IMMUTABLE — box office grosses, streaming hours, chart positions, contract
values: copy every figure verbatim with its units and what it is measured against; never
round, estimate, or invent.

Include `scandal_heat` (1-5) on every claim: 1=routine, 3=interesting, 5=jaw-dropping.

After record_claims succeeds, write 4-8 sentences of research notes pairing each finding
with the SPECIFIC angle that makes it a story — name the players, the stakes, and why it
lands now, not a vague "fans will love this". Flag confirmed facts versus blind items or
single-sourced rumor. If a finding is thin, say so rather than inflating it. Then stop.
""" % {"min_claims": MIN_CLAIMS}


# Per-type system-prompt preambles (plan §3.3 article types). research_synthesis is
# the original Phase-0 prompt; the rest set the beat, then share _EVIDENCE_FOOTER.
PROMPTS: dict[str, str] = {
    "research_synthesis": _SYSTEM_PROMPT,
    "breaking_sighting": (
        "You are the sightings reporter for an autonomous gossip newsroom. A celebrity "
        "was spotted somewhere with someone — WHO, WHERE, WHEN, and with WHOM are the "
        "lead. What were they wearing? What were they doing? Is this a pattern (third "
        "time at this restaurant with this person) or a one-off? Context: have they been "
        "seen together before, and what is their stated relationship status? Separate a "
        "paparazzi-baited staged walk from a genuine candid — a 'surprise' sighting "
        "right outside the photo agency is not a coincidence. Dates, times, and venue "
        "names are IMMUTABLE — copy them verbatim."
        + _EVIDENCE_FOOTER
    ),
    "feud_coverage": (
        "You are the feud desk for an autonomous gossip newsroom. Two or more named "
        "parties are in conflict. What started it (the inciting incident)? What is the "
        "timeline of escalations? Who has said what publicly, and where — tweets, "
        "statements, court filings, 'sources close to'? Are lawyers or PR involved? Has "
        "either party responded, or is one side conspicuously silent? Is there a power "
        "imbalance (A-list vs up-and-comer)? Keep the he-said/she-said straight and "
        "attribute every shot fired to its source. Quotes, tweets, and statements are "
        "IMMUTABLE — copy the wording exactly, typos and all."
        + _EVIDENCE_FOOTER
    ),
    "casting_news": (
        "You are the casting reporter for an autonomous gossip newsroom. An actor or "
        "director is attached to a project. What is the project, and which "
        "studio / network / streamer? Is this a get (an A-lister for a prestige project) "
        "or a paycheck gig? Who else is attached, and at what stage — in talks, signed, "
        "or filming? Context: what was their last project and how did it perform? "
        "Separate a signed deal from a wishlist leak or a 'in early talks' trial "
        "balloon. Deal values, salaries, start dates, and episode / film counts are "
        "IMMUTABLE — quote them exactly."
        + _EVIDENCE_FOOTER
    ),
    "box_office_report": (
        "You are the box office analyst for an autonomous gossip newsroom. Raw numbers "
        "first. How did it do vs projections, vs the previous installment in the "
        "franchise, vs the budget? What is the international split and the per-theater "
        "average? Any records set? For streaming: hours watched, chart position, weeks "
        "on chart. Do not let a studio's spin reframe a soft opening as a win. EVERY "
        "figure — gross, budget, theater count, hours, rank — is IMMUTABLE: copy it "
        "verbatim with its currency and timeframe, and always capture the baseline it is "
        "measured against (a number without its comparison is noise)."
        + _EVIDENCE_FOOTER
    ),
    "blind_item": (
        "You are the blind-item analyst for an autonomous gossip newsroom, working an "
        "anonymous tip or DeuxMoi-style submission. What level of detail is there? Can "
        "you triangulate who it might be from the clues — initials, projects, "
        "'this B-list actor from a beloved sitcom'? Is it from a known-reliable tipster "
        "or a first-time account? What is the implied story? DISTINGUISH CLEARLY: this "
        "is a BLIND ITEM — state in every claim that it is UNCONFIRMED and sourced to a "
        "tip, NOT to a named publication. Do not promote a guess to a fact. Quote the "
        "submission's wording verbatim."
        + _EVIDENCE_FOOTER
    ),
    "relationship_update": (
        "You are the relationships desk for an autonomous gossip newsroom, covering a "
        "status change: new couple, breakup, divorce, engagement, or pregnancy. Who are "
        "the parties? What is the timeline (how long together or married)? Is there "
        "public confirmation — a statement, a rep confirmation, an Instagram post — or "
        "just chatter and a deleted photo? Is there an alleged third party? Kids "
        "involved? A pre-nup? What is the PR strategy behind the rollout (who leaked it "
        "first, and why now)? Separate a rep-confirmed split from a rumor. Dates and "
        "timelines are IMMUTABLE."
        + _EVIDENCE_FOOTER
    ),
    "album_drop": (
        "You are the music reporter for an autonomous gossip newsroom, covering a "
        "release. The artist, the album title, the release date. First-week sales "
        "projections? Lead-single performance? Is a tour bundled with the announcement? "
        "What is the label situation — major, indie, or a fully independent release? "
        "Critical reception if available. Context: how does this compare to their last "
        "release? Separate a real chart story from label hype and a stacked deluxe "
        "edition. Sales figures, streaming counts, and chart positions are IMMUTABLE — "
        "copy them verbatim."
        + _EVIDENCE_FOOTER
    ),
    "fashion_moment": (
        "You are the fashion desk for an autonomous gossip newsroom. WHO wore WHAT by "
        "WHOM at WHICH event — that is the lead. Is the look on-theme or off? Is this a "
        "new brand relationship (just signed, first time wearing the house) or an "
        "established one? What is the price point, and is the piece custom, archival, or "
        "off-the-rack? Capture the beauty details — hair, makeup, jewelry, accessories — "
        "and who styled it. Separate a paid brand ambassadorship from an organic pull. "
        "Prices and any deal values are IMMUTABLE — quote them exactly."
        + _EVIDENCE_FOOTER
    ),
    "career_milestone": (
        "You are the industry desk for an autonomous gossip newsroom, covering a career "
        "milestone: an award win or nomination, a directorial debut, a production deal, "
        "a franchise extension, or a contract renewal. What exactly was achieved, and "
        "(for awards) against whom? What does it mean for their trajectory — is this "
        "overdue and expected, or a genuine surprise? Separate a real inflection point "
        "from a press-release title bump. Deal values, vote tallies, nomination counts, "
        "and dates are IMMUTABLE."
        + _EVIDENCE_FOOTER
    ),
    "viral_moment": (
        "You are the trending desk for an autonomous gossip newsroom. Something blew up "
        "online: what happened, and on which platform? How big — view count, share "
        "count, trending position? Is it organic or manufactured? Who is involved, and "
        "has anyone official responded? Read the lifecycle: still growing, peaked, or "
        "being memed to death? Do not mistake a brand's astroturf for a grassroots "
        "moment. View counts, share counts, and trending ranks are IMMUTABLE — copy "
        "them verbatim with their timestamp."
        + _EVIDENCE_FOOTER
    ),
    "scandal_alert": (
        "You are the scandal desk for an autonomous gossip newsroom. Something BAD "
        "just happened — an arrest, a public meltdown, a leaked tape, a secret exposed, "
        "a walk-off, a firing, a lawsuit, a rehab check-in, a cheating scandal with "
        "receipts. WHO is involved? WHAT exactly happened? WHEN did it go down? WHO "
        "is staying silent (the silence is the story)? Is there a cover-up, a PR "
        "cleanup, a lawyer statement, a 'sources close to' damage-control planted "
        "piece? How bad is this on a 1-10 career-damage scale? Separate the act from "
        "the spin — a 'mutual decision' is a firing, a 'spiritual journey' is rehab, "
        "a 'we remain friends' means someone got dumped. Every quote, statement, and "
        "timestamp is IMMUTABLE."
        + _EVIDENCE_FOOTER
    ),
    "who_wore_it_better": (
        "You are the fashion face-off desk for an autonomous gossip newsroom. Two (or "
        "more) celebrities wore the same look, the same designer, or competing looks "
        "at the same event — or within the same week. WHO wore WHAT by WHOM? Which "
        "event and when? What's the price point — custom, off-the-rack, archival pull? "
        "Who styled each look? Now JUDGE: who wore it better, and WHY? Fit, "
        "styling, accessories, hair and makeup, attitude, the full package. This is a "
        "VERDICT, not a both-sides diplomatic summary — pick a winner and defend the "
        "call. Context: what is each celebrity's style history and brand relationships? "
        "Separate a paid ambassadorship from a genuine pull. Prices, designers, and "
        "event names are IMMUTABLE."
        + _EVIDENCE_FOOTER
    ),
}


def _system_prompt_for(article_type: str) -> str:
    """Return the system-prompt preamble for ``article_type`` (default synthesis).
    
    The prompt's min_claims reference is set at module-load time to the default
    MIN_CLAIMS (5). This is informational — the actual enforcement uses
    :func:`_min_claims_for` in the research() function.
    """
    return PROMPTS.get(article_type, PROMPTS["research_synthesis"])


def _source_briefs(sources: list[Source]) -> str:
    lines: list[str] = []
    for s in sources:
        text = s.cleaned_text or ""
        snippet = text[:280].replace("\n", " ")
        cats = ", ".join(s.categories or []) or "—"
        lines.append(
            f"[source_id={s.id}] \"{s.title or '(untitled)'}\" "
            f"({s.url}) categories={cats}\n  snippet: {snippet}"
        )
    return "\n\n".join(lines)


def _build_user_message(sources: list[Source]) -> str:
    return (
        "Assigned source(s) for this gossip story:\n\n"
        f"{_source_briefs(sources)}\n\n"
        "Read the full text with fetch_url, extract the gossip claims with "
        "verbatim supporting spans, call record_claims, then write your research "
        "notes pairing each finding with the angle that makes it a story."
    )


# --- DB helpers --------------------------------------------------------------

def _load_sources(source_ids: list[int]) -> list[Source]:
    factory = get_sync_session_factory()
    with factory() as session:
        rows = session.execute(
            select(Source).where(Source.id.in_(source_ids))
        ).scalars().all()
    # Preserve the caller's ordering.
    by_id = {s.id: s for s in rows}
    return [by_id[i] for i in source_ids if i in by_id]


def _create_run(article_type: str) -> int:
    factory = get_sync_session_factory()
    with factory() as session:
        run = Run(article_type=article_type, stage="selected")
        session.add(run)
        session.commit()
        return run.id


def _set_stage(run_id: int, stage: str, *, error: str | None = None) -> None:
    factory = get_sync_session_factory()
    with factory() as session:
        run = session.get(Run, run_id)
        if run is not None:
            run.stage = stage
            if error is not None:
                run.error = error
            session.commit()


def record_run_error(run_id: int, error_msg: str) -> None:
    """Record an error against ``run_id`` without bumping attempts.

    Use this for pipeline-stage failures (e.g. draft crash) so audit
    tools can detect stalled runs instead of seeing stage='researched'
    with error=NULL.
    """
    _set_stage(run_id, "error", error=error_msg)
    log.error("run %d errored: %s", run_id, error_msg)


def _claim_ids_from(tool_results: list[dict]) -> list[int]:
    """Pull every claim id out of record_claims results in a tool-call log."""
    ids: list[int] = []
    for inv in tool_results:
        if inv.get("name") != "record_claims":
            continue
        res = inv.get("result") or {}
        for cid in res.get("claim_ids", []) or []:
            if isinstance(cid, int):
                ids.append(cid)
    return ids


def _looks_like_refusal(text: str) -> bool:
    t = (text or "").lower()
    # False positives: "I cannot verify" is honesty, not refusal
    if any(fp in t for fp in _REFUSAL_FALSE_POSITIVES):
        return False
    return any(m in t for m in _REFUSAL_MARKERS)


# --- entry point -------------------------------------------------------------

@traced("research")
def research(
    source_ids: list[int],
    article_type: str = "breaking_sighting",
    *,
    run_id: int | None = None,
    model: str | None = None,
    max_turns: int = MAX_TOOL_TURNS,
    source_classes: list[str] | None = None,
) -> ResearchResult:
    """Run the research stage for ``source_ids`` and return a :class:`ResearchResult`.

    Creates a ``runs`` row (unless ``run_id`` is supplied), drives the native tool
    loop, and locks provenance via ``record_claims``. On refusal / no-claims it
    re-prompts up to twice, then marks the run ``dlq``.

    ``source_classes`` scopes the tool bus's search_sources to these source classes
    (per-vertical isolation). When None, the default arxiv scope is used.
    """
    if not source_ids:
        raise ValueError("research() needs at least one source_id")

    sources = _load_sources(source_ids)
    if not sources:
        raise LookupError(f"no sources found for ids {source_ids}")
    resolved_ids = [s.id for s in sources]

    if run_id is None:
        run_id = _create_run(article_type)
    model = model or settings.model_primary

    client = get_client()
    if not client.providers:
        _set_stage(run_id, "dlq", error="research: no LLM providers configured")
        return ResearchResult(
            run_id=run_id, source_ids=resolved_ids,
            error="no LLM providers configured", model=model,
        )

    bus = PyToolBus(run_id=run_id, source_classes=source_classes)
    tools = tool_specs()

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt_for(article_type)},
        {"role": "user", "content": _build_user_message(sources)},
    ]

    claim_ids: list[int] = []
    notes = ""
    tool_turns = 0
    in_tokens = out_tokens = 0
    attempts = 0
    refused = False

    for attempt in range(MAX_REPROMPTS + 1):
        attempts += 1
        result = client.chat_with_tools(
            messages, model=model, tools=tools, bus=bus, max_turns=max_turns,
        )
        tool_turns += result.tool_turns
        in_tokens += result.in_tokens
        out_tokens += result.out_tokens
        notes = result.text.strip()

        new_ids = _claim_ids_from(result.tool_results)
        if new_ids:
            claim_ids.extend(new_ids)
            log.info(
                "research run=%d attempt=%d recorded %d claims (turns=%d)",
                run_id, attempt + 1, len(new_ids), result.tool_turns,
            )
            break

        refused = _looks_like_refusal(notes)
        log.warning(
            "research run=%d attempt=%d recorded no claims (refusal=%s); re-prompting",
            run_id, attempt + 1, refused,
        )
        if attempt < MAX_REPROMPTS:
            messages = [
                *messages,
                {"role": "assistant", "content": notes or "(no claims recorded)"},
                {"role": "user", "content": _REPROMPT},
            ]

    if not claim_ids:
        err = "research: no claims recorded after re-prompts" + (" (refusal)" if refused else "")
        _set_stage(run_id, "dlq", error=err)
        log.error("research run=%d -> DLQ: %s", run_id, err)
        return ResearchResult(
            run_id=run_id, source_ids=resolved_ids, research_notes=notes,
            tool_turns=tool_turns, model=model, in_tokens=in_tokens,
            out_tokens=out_tokens, attempts=attempts, refused=refused, error=err,
        )

    _set_stage(run_id, "researched")
    return ResearchResult(
        run_id=run_id,
        source_ids=resolved_ids,
        claim_ids=claim_ids,
        research_notes=notes,
        tool_turns=tool_turns,
        model=model,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        attempts=attempts,
        refused=refused,
    )
