"""Model-escalation stage (plan O-C3 / §3.4): re-draft weak articles on a stronger model.

After the first draft is scored by the gate judge, an article whose
``quality_score`` falls below :data:`settings.quality_gate_threshold` is eligible
for *escalation*: a second draft from the same locked evidence base on
``settings.model_escalation`` (a stronger / different model — identical to the
primary in Phase 1, swapped for a larger model later). Escalation is rate-limited
by the per-day ``escalation_cap`` (``budget_day.escalations``) and by the daily
spend ceiling. We re-score the escalated draft with the same gate judge and keep
**whichever draft scores higher** — escalation can never make an article worse.

The decision and both scores are returned on :class:`EscalationResult` so the
run can record exactly what happened in its metadata.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..budget import can_escalate, record_escalation, reserve, settle
from ..config import settings
from ..eval.judge import RubricScores, gate_judge_detailed
from .draft import ArticleEnvelope, draft
from .research import ResearchResult
from ..telemetry import traced

log = logging.getLogger(__name__)

#: Flat USD estimate reserved/settled for one escalation re-draft (draft() does not
#: surface token usage). Conservative; reconciled against the daily ceiling.
ESCALATION_REDRAFT_EST_USD = 0.02


@dataclass(slots=True)
class EscalationResult:
    """Outcome of the escalation decision for one run."""

    envelope: ArticleEnvelope          # the chosen (better-scoring) draft
    scores: RubricScores               # gate scores of the chosen draft
    escalated: bool                    # did we actually re-draft on the escalation model?
    used_escalated: bool               # did the escalated draft win?
    reason: str                        # why we did / didn't escalate
    original_score: float              # gate score of the primary draft
    final_score: float                 # gate score of the chosen draft
    model_used: str                    # model behind the chosen draft

    def as_metadata(self) -> dict:
        """Compact dict for run metadata / logging."""
        return {
            "escalated": self.escalated,
            "used_escalated": self.used_escalated,
            "reason": self.reason,
            "original_score": round(self.original_score, 4),
            "final_score": round(self.final_score, 4),
            "model_used": self.model_used,
        }


@traced("escalation")
def escalate_if_needed(
    run_id: int,
    research_result: ResearchResult,
    envelope: ArticleEnvelope,
    gate_scores: RubricScores,
    *,
    claims: list[str],
    source_spans: list[str],
    article_type: str = "breaking_sighting",
    threshold: float | None = None,
) -> EscalationResult:
    """Escalate the draft for ``run_id`` if its gate score is below threshold.

    ``gate_scores`` is the already-computed gate result for the primary draft (so
    we don't score it twice). Returns an :class:`EscalationResult` whose
    ``envelope``/``scores`` are the better of the primary and (optional) escalated
    drafts. Never raises — a failed re-draft falls back to the primary draft.
    """
    threshold = settings.quality_gate_threshold if threshold is None else threshold
    primary_model = research_result.model or settings.model_primary
    base = EscalationResult(
        envelope=envelope,
        scores=gate_scores,
        escalated=False,
        used_escalated=False,
        reason="",
        original_score=gate_scores.weighted,
        final_score=gate_scores.weighted,
        model_used=primary_model,
    )

    if gate_scores.weighted >= threshold:
        base.reason = f"gate {gate_scores.weighted:.2f} >= threshold {threshold:.2f}; no escalation"
        log.info("escalation run=%d: %s", run_id, base.reason)
        return base

    if not asyncio.run(can_escalate()):
        base.reason = "escalation cap reached for today; keeping primary draft"
        log.info("escalation run=%d: %s", run_id, base.reason)
        return base

    if not asyncio.run(reserve(est_usd=ESCALATION_REDRAFT_EST_USD)):
        base.reason = "daily budget exhausted; cannot afford escalation re-draft"
        log.info("escalation run=%d: %s", run_id, base.reason)
        return base

    # Commit to spending an escalation: count it, then re-draft on the stronger model.
    asyncio.run(record_escalation())
    esc_model = settings.model_escalation
    log.info(
        "escalation run=%d: gate %.2f < %.2f — re-drafting on %s",
        run_id, gate_scores.weighted, threshold, esc_model,
    )
    try:
        esc_envelope = draft(
            run_id, research_result, article_type=article_type, model=esc_model
        )
    except ValueError as exc:
        # Re-draft failed validation: still settle the reserved spend, keep primary.
        asyncio.run(settle(run_id, ESCALATION_REDRAFT_EST_USD))
        base.escalated = True
        base.reason = f"escalation re-draft failed ({exc}); keeping primary draft"
        log.warning("escalation run=%d: %s", run_id, base.reason)
        return base

    esc_scores, esc_result = gate_judge_detailed(esc_envelope.body, claims, source_spans)
    esc_scores.judge_kind = "gate"

    # Settle: flat estimate for the re-draft + the actual gate-judge token cost.
    redraft_cost = ESCALATION_REDRAFT_EST_USD
    if esc_result is not None:
        from ..budget import estimate_cost_usd

        redraft_cost += estimate_cost_usd(
            esc_result.in_tokens, esc_result.out_tokens, esc_result.model
        )
    asyncio.run(settle(run_id, redraft_cost))

    base.escalated = True
    if esc_scores.weighted > gate_scores.weighted:
        base.envelope = esc_envelope
        base.scores = esc_scores
        base.used_escalated = True
        base.final_score = esc_scores.weighted
        base.model_used = esc_result.model if esc_result else esc_model
        base.reason = (
            f"escalated draft {esc_scores.weighted:.2f} beat primary "
            f"{gate_scores.weighted:.2f}; using escalated"
        )
    else:
        base.reason = (
            f"escalated draft {esc_scores.weighted:.2f} did not beat primary "
            f"{gate_scores.weighted:.2f}; keeping primary"
        )
    log.info("escalation run=%d: %s", run_id, base.reason)
    return base
