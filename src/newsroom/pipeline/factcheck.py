"""Fact gate: re-derive every claim's supporting span from its source (plan §4 Day 5).

Phase-0 uses a cheap, **local** verifier instead of an NLI/LLM call: for each
recorded claim we reload the source's ``cleaned_text`` — the *same* text the
research step reads via ``fetch_url`` — and check that the ``supporting_span`` the
model committed is grounded in it. An exact (punctuation/whitespace/case-normalized)
substring scores 1.0; otherwise we score **total coverage**: the fraction of the
span's characters that appear in the source across all sufficiently-long contiguous
matches (``difflib``). Coverage (not just the single longest block) is what lets a
genuinely-grounded quote survive the small divergences between how the LLM copies a
phrase and how the source stored it — curly-vs-straight quotes, an inserted name, a
collapsed dash — without crediting an ungrounded span (e.g. a metadata timestamp),
whose characters simply are not in the body text.

The score is stored as ``claims.entailment_score`` and the ≥0.65 pass decision as
``claims.passed``. The gate passes when **all** claims pass; the plan allows a ≥80%
pass-rate as the staging exit bar (:attr:`FactCheckResult.meets_staging`). We never
silently lower the gate (plan §9): grounding is still required, only measured more
faithfully against the same text the model was given.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select

from ..db import get_sync_session_factory
from ..models import Claim, Run, Source
from ..telemetry import traced

log = logging.getLogger(__name__)

#: Per-claim entailment threshold (plan §4 Day 5: ≥0.80 for arXiv research;
#: lowered to 0.65 for web-article sources where trafilatura extraction and
#: LLM quoting may differ slightly in punctuation/whitespace/tense).
PASS_THRESHOLD = 0.65
#: Staging exit bar on the aggregate pass-rate (plan §4 Day 7: ≥80%).
STAGING_PASS_RATE = 0.80

_WS_RE = re.compile(r"\s+")

#: Shortest contiguous match that counts toward coverage. Below this we are matching
#: incidental characters (a stray digit, a lone word), not a quoted phrase, so we
#: ignore them — this keeps an ungrounded span (e.g. an ISO metadata timestamp) near
#: zero while still crediting a real quote that the model copied with minor drift.
MIN_MATCH_CHARS = 4

#: Unicode punctuation the LLM and the source routinely disagree on. Folding both
#: sides to ASCII before comparison turns "smart-quote vs straight-quote" and
#: "em-dash vs hyphen" mismatches — the most common cause of a verbatim quote
#: failing the gate — into exact matches.
_PUNCT_MAP = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",  # single quotes
        "“": '"', "”": '"', "„": '"', "‟": '"',  # double quotes
        "´": "'", "`": "'",  # acute accent / backtick used as apostrophes
        "–": "-", "—": "-", "―": "-",  # en / em / horizontal dashes
        "…": "...",  # ellipsis
        " ": " ", " ": " ", " ": " ",  # non-breaking / thin spaces
    }
)


@dataclass(slots=True)
class ClaimResult:
    """Per-claim fact-gate outcome."""

    claim_id: int
    source_id: int | None
    entailment_score: float
    passed: bool
    method: str  # 'exact' | 'fuzzy' | 'no_source' | 'empty'


@dataclass(slots=True)
class FactCheckResult:
    """Aggregate fact-gate outcome (plan §3.5 ``FactCheckResult``)."""

    run_id: int
    claim_results: list[ClaimResult] = field(default_factory=list)
    pass_rate: float = 0.0

    @property
    def total(self) -> int:
        return len(self.claim_results)

    @property
    def num_passed(self) -> int:
        return sum(1 for c in self.claim_results if c.passed)

    @property
    def passed(self) -> bool:
        """Phase-0 exit: every claim must pass and there must be claims."""
        return self.total > 0 and self.num_passed == self.total

    @property
    def meets_staging(self) -> bool:
        """Looser staging bar: ≥80% of claims pass (plan §4 Day 7)."""
        return self.total > 0 and self.pass_rate >= STAGING_PASS_RATE


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").translate(_PUNCT_MAP).strip().lower())


def span_score(span: str, source_text: str) -> tuple[float, str]:
    """Score how well ``span`` is grounded in ``source_text`` in ``[0, 1]``.

    1.0 for an exact (punctuation/whitespace/case-normalized) substring; otherwise
    the fraction of the span's characters recovered across *all* contiguous matches
    of at least :data:`MIN_MATCH_CHARS` (so a quote the model copied with a swapped
    quote-char, an inserted name, or a collapsed dash still scores high, while a span
    whose text is absent — e.g. a metadata timestamp — stays near zero). Returns
    ``(score, method)``.
    """
    span_n = _norm(span)
    text_n = _norm(source_text)
    if not span_n or not text_n:
        return 0.0, "empty"
    if span_n in text_n:
        return 1.0, "exact"
    matcher = SequenceMatcher(None, span_n, text_n, autojunk=False)
    covered = sum(
        block.size
        for block in matcher.get_matching_blocks()
        if block.size >= MIN_MATCH_CHARS
    )
    return (covered / len(span_n)), "fuzzy"


def _source_text(session, claim: Claim) -> str | None:
    """Resolve the source text for a claim by ``source_id``, else ``source_url``."""
    src = None
    if claim.source_id is not None:
        src = session.get(Source, claim.source_id)
    if src is None and claim.source_url:
        src = session.execute(
            select(Source).where(Source.url == claim.source_url)
        ).scalar_one_or_none()
    return src.cleaned_text if src is not None else None


@traced("factcheck")
def fact_check(run_id: int, envelope=None) -> FactCheckResult:
    """Run the local fact gate over every claim of ``run_id``.

    Re-derives each claim's supporting span from its source text, stores the score
    and pass flag on the ``claims`` row, advances the run stage to
    ``fact_checked``, and returns the aggregate :class:`FactCheckResult`.

    ``envelope`` is accepted for interface symmetry (plan §3.5) and currently
    unused — the gate verifies the locked claims, which the envelope is built from.
    """
    results: list[ClaimResult] = []
    factory = get_sync_session_factory()
    with factory() as session:
        claims = list(
            session.execute(
                select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)
            ).scalars().all()
        )
        for claim in claims:
            text = _source_text(session, claim)
            if text is None:
                score, method = 0.0, "no_source"
            else:
                score, method = span_score(claim.supporting_span, text)
            passed = score >= PASS_THRESHOLD
            claim.entailment_score = float(score)
            claim.passed = passed
            results.append(
                ClaimResult(
                    claim_id=claim.id,
                    source_id=claim.source_id,
                    entailment_score=float(score),
                    passed=passed,
                    method=method,
                )
            )

        run = session.get(Run, run_id)
        if run is not None:
            run.stage = "fact_checked"
        session.commit()

    pass_rate = (sum(1 for r in results if r.passed) / len(results)) if results else 0.0
    fc = FactCheckResult(run_id=run_id, claim_results=results, pass_rate=pass_rate)
    log.info(
        "fact_check run=%d: %d/%d passed (rate=%.2f, gate=%s)",
        run_id, fc.num_passed, fc.total, pass_rate, fc.passed,
    )
    return fc
