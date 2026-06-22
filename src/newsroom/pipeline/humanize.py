"""Humanizer stage (plan §4 / vertical amendment V4): readability without fact drift.

The humanizer is a style-transfer pass that rewrites a fact-gated draft to read
more naturally, then *verifies* that the rewrite changed nothing that matters:

1. :func:`humanize` — DeepSeek style transfer. The prompt is explicit that numbers,
   names, quotes and technical terms are IMMUTABLE; only flow, sentence variety and
   tone may change.
2. :func:`verify_humanize` — the safety net. It NER-extracts numbers, proper nouns
   and quoted spans from both texts and set-compares them (fuzzy), re-runs the fact
   gate logic on the humanized text against the locked source spans, and checks that
   every ``[^n]`` citation marker still resolves. Any drift fails the check.

The contract (enforced by the caller): if verification fails, the **original**
draft is published — the humanized text is discarded. This makes the humanizer a
strictly-safe optimisation: it can only ever improve readability, never accuracy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..config import settings
from ..db import get_sync_session_factory
from ..llm import ChatResult, LLMError, get_client
from ..models import Eval
from ..telemetry import traced
from .factcheck import span_score

log = logging.getLogger(__name__)

#: Fraction of claims whose locked facts must survive into the humanized text.
FACT_PASS_MIN = 0.95
#: Fuzzy presence threshold (difflib ratio) for a name/quote surviving the rewrite.
PRESENCE_THRESHOLD = 0.90

_HUMANIZE_SYSTEM = (
    "You are a senior copy editor for a gossip and entertainment news publication. "
    "You make sharp, snappy prose read like a person wrote it — never blander, never "
    "more generic. You have a trained ear for the tells of machine-written text and "
    "you delete them on sight."
)

_HUMANIZE_INSTRUCTION = """\
Rewrite this gossip article so it reads like a sharp, well-connected insider wrote it —
more natural, varied, and direct — without changing a single fact.

Do not change any facts. Only improve flow, sentence variety, rhythm, and tone.
Make it less generic, not more: keep the edge, the specificity, and the point of view.

Strip the tells of AI writing:
- Kill formulaic transitions: "Moreover", "Furthermore", "Additionally", "It is worth
  noting that", "In conclusion".
- Break the rule of three — do not let every list or sentence arrive in tidy triples.
- Cut "not just X but Y" and "it's not about X, it's about Y" constructions.
- Drop inflated throat-clearing ("In the rapidly evolving world of...") and hollow
  closers that only restate the sentence before them.
- Avoid empty vocabulary: "delve", "tapestry", "testament", "underscores", "pivotal",
  "crucial", "landscape", "realm", "showcase", "seamless".
- Do not lean on the em dash; vary sentence length instead of repeating one rhythm.
- Replace vague attributions ("experts say", "many believe") with the concrete source
  already in the text, or cut them.
- Prefer plain verbs over nominalizations ("decides" over "makes a decision").

Hard constraints:
- Every figure, percentage, ticker, model name, protocol name and proper noun must
  appear unchanged. Do not round, convert units, or rephrase a quoted span.
- Keep any [^n] citation markers exactly where they are.
- Keep it the same article: same claims, same structure of evidence, same length band.
  Add no facts, examples, or numbers that are not already present. Output ONLY the
  rewritten article body in Markdown — no preamble, no fences.

Article ({article_type}):
---
{body}
---"""


# --- NER-ish extraction (regex; no heavyweight model dependency) -------------

# A numeric token: optional currency, digits with separators, optional decimal,
# optional %/x/× suffix or magnitude letter (K/M/B/T). Captures the things that
# must stay immutable (V4): benchmark figures, prices, percentages, parameter counts.
_NUMBER_RE = re.compile(
    r"[$€£]?\d[\d,]*(?:\.\d+)?\s?(?:%|x|×|bps|[KMBT]\b)?",
    re.IGNORECASE,
)
# Proper-noun candidates: capitalised word runs, dotted/hyphenated names, and
# ALL-CAPS acronyms / tickers (BTC, ETH, ZK, TEE, GPT-4).
_PROPER_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9]*(?:[.\-][A-Za-z0-9]+)*"
    r"(?:\s+[A-Z][a-zA-Z0-9]*(?:[.\-][A-Za-z0-9]+)*)*)\b"
)
_QUOTE_RE = re.compile(r"[\"“”']{1}([^\"“”']{4,})[\"“”']{1}")
_CITATION_RE = re.compile(r"\[\^[A-Za-z0-9_-]+\]")

# Capitalised words that are almost always sentence-initial / function words, not
# names — excluded to keep proper-noun drift detection from firing on prose.
_PROPER_STOPWORDS = frozenset(
    {
        "the", "this", "that", "these", "those", "a", "an", "and", "but", "or",
        "if", "in", "on", "at", "to", "for", "of", "with", "by", "as", "is", "are",
        "was", "were", "it", "we", "they", "he", "she", "you", "i", "our", "their",
        "however", "moreover", "meanwhile", "while", "when", "where", "what", "why",
        "how", "here", "there", "then", "thus", "therefore", "because", "although",
        "despite", "yet", "so", "also", "both", "each", "its", "his", "her",
    }
)


def _norm_number(tok: str) -> str:
    return tok.replace(",", "").replace(" ", "").strip().lower().rstrip(".")


def extract_numbers(text: str) -> set[str]:
    """Extract normalised numeric tokens (the immutable figures) from ``text``."""
    out: set[str] = set()
    for m in _NUMBER_RE.findall(text or ""):
        norm = _norm_number(m)
        # Drop a bare result like "" or a lone separator.
        if norm and any(ch.isdigit() for ch in norm):
            out.add(norm)
    return out


def extract_proper_nouns(text: str) -> set[str]:
    """Extract proper-noun candidates (names, acronyms, tickers) from ``text``."""
    out: set[str] = set()
    for m in _PROPER_RE.findall(text or ""):
        m = m.strip()
        if not m:
            continue
        # A single short, lowercase-when-folded stopword (sentence-initial) is noise.
        if " " not in m and m.lower() in _PROPER_STOPWORDS:
            continue
        if len(m) < 2:
            continue
        out.add(m)
    return out


def extract_quotes(text: str) -> list[str]:
    """Extract quoted spans (≥4 chars) that must be reproduced verbatim."""
    return [q.strip() for q in _QUOTE_RE.findall(text or "") if q.strip()]


def extract_citations(text: str) -> set[str]:
    """Extract ``[^n]`` citation markers from ``text``."""
    return set(_CITATION_RE.findall(text or ""))


def _present_in(needle: str, haystack: str) -> bool:
    """True if ``needle`` survives into ``haystack`` (exact or fuzzy ≥ threshold)."""
    if not needle:
        return True
    score, _ = span_score(needle, haystack)
    return score >= PRESENCE_THRESHOLD


def _claim_span(claim) -> str:
    """Duck-type a claim into its supporting span / text for fact re-checking."""
    if isinstance(claim, str):
        return claim
    if isinstance(claim, dict):
        return claim.get("supporting_span") or claim.get("claim_text") or ""
    return getattr(claim, "supporting_span", None) or getattr(claim, "claim_text", "") or ""


# --- verification ------------------------------------------------------------

@dataclass(slots=True)
class HumanizeVerifyResult:
    """Outcome of the post-humanize verification pass."""

    passed: bool
    drift_detected: bool
    drift_details: dict = field(default_factory=dict)
    fact_pass_rate: float = 1.0
    citation_intact: bool = True
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "drift_detected": self.drift_detected,
            "drift_details": self.drift_details,
            "fact_pass_rate": round(self.fact_pass_rate, 4),
            "citation_intact": self.citation_intact,
            "reason": self.reason,
        }


def verify_humanize(original: str, humanized: str, claims: list) -> HumanizeVerifyResult:
    """Verify a humanized rewrite introduced no factual / citation drift.

    (a) NER-extract numbers + proper nouns + quoted spans from both texts and
        set-compare them (fuzzy), flagging anything dropped or altered;
    (b) re-run the fact gate over the humanized text against the same locked source
        spans (every claim's immutable facts must survive);
    (c) verify all ``[^n]`` citation markers still resolve.

    Fails (``passed=False``) on any drift, sub-threshold fact survival, or a dropped
    citation marker — the caller then keeps the original draft.
    """
    # (a) immutable-token drift.
    nums_orig, nums_hum = extract_numbers(original), extract_numbers(humanized)
    missing_numbers = sorted(nums_orig - nums_hum)

    dropped_nouns = sorted(
        n for n in extract_proper_nouns(original) if not _present_in(n, humanized)
    )
    altered_quotes = [q for q in extract_quotes(original) if not _present_in(q, humanized)]

    # (b) fact gate over the humanized text against the locked spans.
    spans = [s for s in (_claim_span(c) for c in (claims or [])) if s]
    survived = 0
    failed_claims: list[str] = []
    for span in spans:
        span_nums = extract_numbers(span)
        span_names = extract_proper_nouns(span)
        nums_ok = span_nums <= nums_hum
        names_ok = all(_present_in(n, humanized) for n in span_names)
        if nums_ok and names_ok:
            survived += 1
        else:
            failed_claims.append(span[:120])
    fact_pass_rate = (survived / len(spans)) if spans else 1.0

    # (c) citation markers.
    cites_orig, cites_hum = extract_citations(original), extract_citations(humanized)
    dropped_citations = sorted(cites_orig - cites_hum)
    citation_intact = not dropped_citations

    drift_detected = bool(missing_numbers or dropped_nouns or altered_quotes)
    drift_details = {
        "missing_numbers": missing_numbers,
        "dropped_proper_nouns": dropped_nouns,
        "altered_quotes": altered_quotes,
        "failed_claims": failed_claims,
        "dropped_citations": dropped_citations,
    }

    passed = (
        not drift_detected
        and citation_intact
        and fact_pass_rate >= FACT_PASS_MIN
    )
    reason = "clean" if passed else "; ".join(
        part for part in (
            f"missing_numbers={missing_numbers}" if missing_numbers else "",
            f"dropped_nouns={len(dropped_nouns)}" if dropped_nouns else "",
            f"altered_quotes={len(altered_quotes)}" if altered_quotes else "",
            f"fact_pass_rate={fact_pass_rate:.2f}" if fact_pass_rate < FACT_PASS_MIN else "",
            f"dropped_citations={dropped_citations}" if dropped_citations else "",
        ) if part
    )
    log.info(
        "humanize verify: passed=%s drift=%s fact_rate=%.2f citations=%s",
        passed, drift_detected, fact_pass_rate, citation_intact,
    )
    return HumanizeVerifyResult(
        passed=passed,
        drift_detected=drift_detected,
        drift_details=drift_details,
        fact_pass_rate=fact_pass_rate,
        citation_intact=citation_intact,
        reason=reason,
    )


# --- humanizer ---------------------------------------------------------------

def humanize_detailed(
    article_text: str, article_type: str = "breaking_sighting", *, model: str | None = None
) -> tuple[str, ChatResult | None]:
    """Style-transfer rewrite; returns (humanized_text, raw_result). Falls back to input."""
    client = get_client()
    messages = [
        {"role": "system", "content": _HUMANIZE_SYSTEM},
        {
            "role": "user",
            "content": _HUMANIZE_INSTRUCTION.format(
                article_type=article_type, body=article_text
            ),
        },
    ]
    try:
        result = client.chat(
            messages,
            model=model or settings.model_primary,
            max_tokens=2600,
            temperature=0.7,
        )
    except LLMError as exc:
        log.warning("humanize failed (%s); returning original text", exc)
        return article_text, None
    text = (result.text or "").strip()
    return (text or article_text), result


@traced("humanize")
def humanize(article_text: str, article_type: str = "breaking_sighting") -> str:
    """Rewrite ``article_text`` for naturalness/readability (facts immutable)."""
    text, _ = humanize_detailed(article_text, article_type)
    return text


def record_humanize(article_id: int, verify: HumanizeVerifyResult) -> int:
    """Persist a humanize verification outcome as an ``evals`` row (pass/fail tracking)."""
    factory = get_sync_session_factory()
    with factory() as session:
        row = Eval(
            article_id=article_id,
            judge_kind="humanize",
            judge_model="ner-verify",
            scores_json=verify.as_dict(),
            weighted=1.0 if verify.passed else 0.0,
        )
        session.add(row)
        session.commit()
        return row.id
