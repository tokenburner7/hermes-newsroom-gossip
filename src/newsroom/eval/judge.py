"""Independent quality judges for the eval harness (plan O-C2, vertical amendment V7).

Two judges, deliberately different in cost and independence:

* :func:`gate_judge` — the *per-article* quality gate. Runs on the PRIMARY model
  (DeepSeek), is cheap and fast, and returns a single weighted score in ``[0, 1]``.
  It drives the escalation decision and is stored as ``articles.quality_score``.

* :func:`eval_judge` — the *sampled, independent* auditor. Routed to a DIFFERENT
  model family via OpenRouter so the newsroom never grades its own homework, and
  returns the full :class:`RubricScores`. If OpenRouter / ``model_eval_judge`` is
  not configured it falls back to the primary model with a note logged.

Both score the same AI×Crypto rubric (vertical amendment V7):

    accuracy 0.35 · coverage 0.20 · coherence 0.10 ·
    citation_integrity 0.20 · style 0.05 · originality 0.10

The weights sum to 1.0; the weighted total is computed deterministically in Python
(:func:`weighted_total`) rather than trusting the model to do the arithmetic.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..llm import ChatResult, LLMError, get_client
from ..telemetry import traced

log = logging.getLogger(__name__)

#: AI×Crypto rubric weights (vertical amendment V7). Must sum to 1.0.
RUBRIC_WEIGHTS: dict[str, float] = {
    "accuracy": 0.35,
    "coverage": 0.20,
    "coherence": 0.10,
    "citation_integrity": 0.20,
    "style": 0.05,
    "originality": 0.10,
}

#: Canonical ordering of the rubric criteria.
CRITERIA: tuple[str, ...] = tuple(RUBRIC_WEIGHTS)


@dataclass(slots=True)
class RubricScores:
    """Per-criterion judge scores in ``[0, 1]`` plus the weighted total."""

    accuracy: float = 0.0
    coverage: float = 0.0
    coherence: float = 0.0
    citation_integrity: float = 0.0
    style: float = 0.0
    originality: float = 0.0
    weighted: float = 0.0
    judge_kind: str = ""
    judge_model: str = ""
    rationale: str = ""
    note: str = ""
    # False when the judge call failed / could not be parsed (scores are a
    # conservative fallback, not a real assessment).
    ok: bool = True

    def criteria(self) -> dict[str, float]:
        """The six rubric criteria as a ``{name: score}`` mapping."""
        return {c: float(getattr(self, c)) for c in CRITERIA}

    def scores_json(self) -> dict:
        """JSON-serialisable payload for the ``evals.scores_json`` column."""
        out = self.criteria()
        out["weighted"] = round(float(self.weighted), 4)
        if self.rationale:
            out["rationale"] = self.rationale
        if self.note:
            out["note"] = self.note
        out["ok"] = self.ok
        return out

    def as_dict(self) -> dict:
        return asdict(self)


def weighted_total(scores: dict[str, float]) -> float:
    """Weighted sum of ``scores`` using :data:`RUBRIC_WEIGHTS`, clamped to ``[0, 1]``."""
    total = 0.0
    for crit, weight in RUBRIC_WEIGHTS.items():
        total += weight * _coerce_unit(scores.get(crit, 0.0))
    return max(0.0, min(1.0, total))


def _coerce_unit(value) -> float:
    """Coerce a model-emitted score to ``[0, 1]``, tolerating 0-5 / 0-10 / 0-100 scales."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0.0
    if x < 0:
        return 0.0
    if x <= 1.0:
        return x
    if x <= 5.0:
        return x / 5.0
    if x <= 10.0:
        return x / 10.0
    if x <= 100.0:
        return x / 100.0
    return 1.0


# --- prompt ------------------------------------------------------------------

_RUBRIC_BLOCK = "\n".join(
    f"- {crit} (weight {RUBRIC_WEIGHTS[crit]:.2f}): {desc}"
    for crit, desc in {
        "accuracy": "every figure/claim in the article is faithful to the evidence; "
        "no number is altered, invented, or contradicted",
        "coverage": "the article covers the important locked claims, not a thin subset, "
        "and does not bury the lead claim",
        "coherence": "the argument flows and builds; structure and transitions make "
        "sense; it reads as one argument, not a list",
        "citation_integrity": "every factual assertion is traceable to a locked claim / "
        "source span; nothing is asserted that the evidence does not support",
        "style": "clear, sharp, insider gossip voice; specific not "
        "generic; no clickbait, no hype words, no AI filler",
        "originality": "genuine synthesis: each finding is paired with a specific, "
        "non-obvious angle (named players, stakes, pattern). A flat "
        "summary of the source, or a vague 'fans will love this' gesture, scores low",
    }.items()
)

_SYSTEM_PROMPT = f"""\
You are an INDEPENDENT quality judge for an autonomous gossip newsroom. You did
not write the article; your job is to grade it strictly against a locked evidence
base, not to be charitable.

Score each rubric criterion on a 0.0-1.0 scale (1.0 = excellent, 0.0 = unacceptable):
{_RUBRIC_BLOCK}

Anchor your scores: 1.0 = no flaw of this kind; 0.8 = minor issues a sharp editor would
note; 0.5 = a real, article-level weakness; 0.2 = a serious failure; 0.0 = unacceptable.
Do NOT cluster every criterion around 0.7 — spread the scores and justify the lowest one
in the rationale.

Be especially harsh on accuracy and citation_integrity: a single altered number or an
assertion the evidence does not support should pull those scores well below 0.5.

Return a SINGLE JSON object (no prose, no markdown fences) with exactly these keys:
{{
  "accuracy": 0.0,
  "coverage": 0.0,
  "coherence": 0.0,
  "citation_integrity": 0.0,
  "style": 0.0,
  "originality": 0.0,
  "rationale": "one short sentence, plain text"
}}
Every value for the six criteria must be a number between 0.0 and 1.0. Keep the
rationale under 200 characters and do NOT use double-quote characters inside it."""


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _evidence_block(claims: list[str], source_spans: list[str]) -> str:
    claims = claims or []
    source_spans = source_spans or []
    claim_lines = "\n".join(f"  C{i}. {_truncate(c, 400)}" for i, c in enumerate(claims, 1))
    span_lines = "\n".join(f"  S{i}. \"{_truncate(s, 400)}\"" for i, s in enumerate(source_spans, 1))
    return (
        "Locked claims (the article must be faithful to these and cover them):\n"
        + (claim_lines or "  (none provided)")
        + "\n\nVerbatim supporting source spans (the ground truth for accuracy):\n"
        + (span_lines or "  (none provided)")
    )


def _build_messages(article_text: str, claims: list[str], source_spans: list[str]) -> list[dict]:
    user = (
        _evidence_block(claims, source_spans)
        + "\n\nARTICLE UNDER REVIEW:\n"
        + _truncate(article_text, 9000)
        + "\n\nGrade the article now and return only the JSON object."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _strip_fences(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return raw


def _parse_scores(raw: str) -> tuple[dict[str, float], str]:
    """Parse a judge reply into ``({criterion: unit_score}, rationale)``.

    Prefers a clean JSON parse; if the model breaks JSON (e.g. an unescaped quote
    inside the rationale), falls back to regex-salvaging each criterion's number —
    the scores are what drive the weighted total, so we recover them when we can.
    """
    raw = _strip_fences(raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            scores = {c: _coerce_unit(data.get(c, 0.0)) for c in CRITERIA}
            return scores, str(data.get("rationale", "") or "")[:600]
    except json.JSONDecodeError:
        pass

    salvaged: dict[str, float] = {}
    for crit in CRITERIA:
        m = re.search(rf'"{crit}"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
        if m is None:
            raise ValueError(f"could not parse score for {crit!r}")
        salvaged[crit] = _coerce_unit(m.group(1))
    mr = re.search(r'"rationale"\s*:\s*"([^"]*)"', raw)
    return salvaged, (mr.group(1)[:600] if mr else "(rationale unparsed)")


def _score(
    article_text: str,
    claims: list[str],
    source_spans: list[str],
    *,
    model: str,
    provider: str | None,
    judge_kind: str,
    note: str = "",
    max_tokens: int = 512,
) -> tuple[RubricScores, ChatResult | None]:
    """Run one judge pass; never raises — returns a flagged fallback on failure."""
    messages = _build_messages(article_text, claims, source_spans)
    client = get_client()
    try:
        result = client.chat(
            messages,
            model=model,
            provider=provider,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=0.0,
        )
        scores, rationale = _parse_scores(result.text.strip())
    except (LLMError, json.JSONDecodeError, ValueError) as exc:
        log.warning("%s judge failed (model=%s): %s", judge_kind, model, exc)
        rs = RubricScores(
            judge_kind=judge_kind,
            judge_model=model,
            note=(note + " | " if note else "") + f"judge unavailable: {type(exc).__name__}",
            ok=False,
        )
        rs.weighted = 0.0
        return rs, None

    rs = RubricScores(
        **{c: scores[c] for c in CRITERIA},
        weighted=weighted_total(scores),
        judge_kind=judge_kind,
        judge_model=result.model or model,
        rationale=rationale,
        note=note,
        ok=True,
    )
    log.info(
        "%s judge model=%s weighted=%.3f (acc=%.2f cov=%.2f cit=%.2f)",
        judge_kind, rs.judge_model, rs.weighted, rs.accuracy, rs.coverage, rs.citation_integrity,
    )
    return rs, result


# --- public judges -----------------------------------------------------------

@traced("gate_judge")
def gate_judge_detailed(
    article_text: str,
    claims: list[str] | None = None,
    source_spans: list[str] | None = None,
    *,
    model: str | None = None,
) -> tuple[RubricScores, ChatResult | None]:
    """Per-article gate judge on the primary model; returns scores + raw result."""
    return _score(
        article_text,
        claims or [],
        source_spans or [],
        model=model or settings.model_primary,
        provider=None,
        judge_kind="gate",
        max_tokens=512,
    )


def gate_judge(
    article_text: str,
    claims: list[str] | None = None,
    source_spans: list[str] | None = None,
    *,
    model: str | None = None,
) -> float:
    """Fast per-article quality gate on the primary model. Returns a weighted ``[0, 1]`` score."""
    rs, _ = gate_judge_detailed(article_text, claims, source_spans, model=model)
    return rs.weighted


def _resolve_eval_target(model: str | None) -> tuple[str, str | None, str]:
    """Pick (model, provider, note) for the eval judge, preferring a cross-family route."""
    chosen = model or settings.model_eval_judge
    if settings.openrouter_configured and chosen:
        return chosen, "openrouter", ""
    note = (
        "eval judge fell back to the primary model — set OPENROUTER_API_KEY and "
        "MODEL_EVAL_JUDGE for an independent cross-family judge"
    )
    log.warning(note)
    return settings.model_primary, None, note


def eval_judge_detailed(
    article_text: str,
    claims: list[str] | None = None,
    source_spans: list[str] | None = None,
    *,
    model: str | None = None,
) -> tuple[RubricScores, ChatResult | None]:
    """Independent cross-family eval judge; returns full scores + raw result."""
    eval_model, provider, note = _resolve_eval_target(model)
    return _score(
        article_text,
        claims or [],
        source_spans or [],
        model=eval_model,
        provider=provider,
        judge_kind="eval",
        note=note,
        max_tokens=768,
    )


def eval_judge(
    article_text: str,
    claims: list[str] | None = None,
    source_spans: list[str] | None = None,
    *,
    model: str | None = None,
) -> RubricScores:
    """Sampled, independent eval judge. Cross-family via OpenRouter; falls back to primary."""
    rs, _ = eval_judge_detailed(article_text, claims, source_spans, model=model)
    return rs
