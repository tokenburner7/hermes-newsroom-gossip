"""Temporal workflow + activities for the newsroom pipeline (plan §6, Phase 2A).

The Phase-0/1 pipeline is a chain of pure-ish stage functions (research → draft →
gate → escalation → fact-gate → humanize → persist → publish). This module wraps
each stage as a Temporal **activity** and chains them in :class:`PipelineWorkflow`,
turning the §3.4 state machine into a durable workflow: crash-resume and
replay-a-run-for-eval come for free, and per-activity retry policies replace the
ad-hoc try/except in ``run-once``.

Design notes
------------
* **Activities are sync** and call the existing sync pipeline functions directly;
  the worker runs them in a :class:`~concurrent.futures.ThreadPoolExecutor` (see
  :mod:`newsroom.temporal_worker`). Async budget helpers are driven with
  ``asyncio.run`` exactly as ``cli.py`` does today.
* **Activities exchange JSON-friendly dicts** (and a small :class:`RunConfig`
  dataclass), never pydantic models — the pydantic ``ArticleEnvelope`` is passed as
  ``model_dump()`` and revalidated inside each activity. This keeps the default
  Temporal data converter happy without the pydantic converter.
* **Idempotency** (plan §6): ``research`` rebuilds claims from scratch, ``persist``
  no-ops if an article already exists for the run, and ``publish`` is already
  two-phase/idempotent on ``status``. Heavy imports are lazy (inside activity
  bodies) so the workflow sandbox stays clean.
* **Failure routing**: research failure after retries routes the run to the DLQ
  (``runs.stage='dlq'``); the kill-switch and an exhausted budget fail the workflow
  fast with a non-retryable error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

log = logging.getLogger(__name__)


@dataclass
class RunConfig:
    """Input to :class:`PipelineWorkflow` — one pipeline attempt = one article."""

    source_id: int
    article_type: str = "breaking_sighting"
    humanize: bool = False
    publish: bool = False


# --- timeouts + retry policies ----------------------------------------------

# LLM stages can take minutes; IO/DB stages are quick. Retries are bounded so a
# persistently failing stage surfaces rather than looping forever.
_LLM_TIMEOUT = timedelta(minutes=5)
_IO_TIMEOUT = timedelta(seconds=90)
_PERSIST_TIMEOUT = timedelta(minutes=5)  # first embed call loads the bge model

_LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)
_IO_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=3,
)


# --- DB helpers (lazy imports; run inside activities only) -------------------

def _set_stage(run_id: int, stage: str, *, error: str | None = None) -> None:
    from .db import get_sync_session_factory
    from .models import Run

    factory = get_sync_session_factory()
    with factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.stage = stage
        if error is not None:
            run.error = error
        session.commit()


def _load_run_claims(run_id: int) -> tuple[list[str], list[str]]:
    """Return (claim_texts, supporting_spans) for a run's locked claims."""
    from sqlalchemy import select

    from .db import get_sync_session_factory
    from .models import Claim

    factory = get_sync_session_factory()
    with factory() as session:
        claims = list(
            session.execute(
                select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)
            ).scalars().all()
        )
    return [c.claim_text for c in claims], [c.supporting_span for c in claims]


# --- activities --------------------------------------------------------------

@activity.defn(name="start_run")
def start_run_activity(cfg: RunConfig) -> int:
    """Kill-switch gate + budget reservation + create the ``runs`` row. Returns run_id."""
    import asyncio

    from .budget import ensure_budget_day, kill_switch_active, reserve
    from .db import get_sync_session_factory
    from .models import Run

    if asyncio.run(kill_switch_active()):
        raise ApplicationError(
            "kill-switch is ON; refusing to start a run",
            type="KillSwitch", non_retryable=True,
        )

    factory = get_sync_session_factory()
    with factory() as session:
        run = Run(article_type=cfg.article_type, stage="selected")
        session.add(run)
        session.commit()
        run_id = run.id

    asyncio.run(ensure_budget_day())
    if not asyncio.run(reserve(est_usd=0.02)):
        _set_stage(run_id, "dlq", error="daily budget exhausted at reservation")
        raise ApplicationError(
            "daily budget exhausted — reservation denied",
            type="BudgetExhausted", non_retryable=True,
        )
    activity.logger.info("start_run: run_id=%s source_id=%s", run_id, cfg.source_id)
    return run_id


@activity.defn(name="research")
def research_activity(run_id: int, cfg: RunConfig) -> dict:
    """Drive the native tool loop and lock provenance. Idempotent: rebuilds claims."""
    from dataclasses import asdict

    from sqlalchemy import delete

    from .db import get_sync_session_factory
    from .models import Claim
    from .pipeline import research as run_research

    # Idempotent rebuild: clear claims from any prior crashed attempt for this run
    # so a retry can't double-record provenance (plan §6: idempotent activities).
    factory = get_sync_session_factory()
    with factory() as session:
        session.execute(delete(Claim).where(Claim.run_id == run_id))
        session.commit()

    result = run_research([cfg.source_id], cfg.article_type, run_id=run_id)
    if not result.ok:
        # A refusal won't self-heal; transient (no providers / empty) may. Either way
        # exhausting retries lets the workflow route the run to the DLQ.
        raise ApplicationError(
            result.error or "research recorded no claims",
            type="ResearchFailed", non_retryable=bool(result.refused),
        )
    activity.logger.info(
        "research: run_id=%s claims=%d turns=%d", run_id, len(result.claim_ids), result.tool_turns
    )
    return asdict(result)


@activity.defn(name="draft")
def draft_activity(research: dict, cfg: RunConfig) -> dict:
    """Draft the article envelope (JSON mode) from the locked claims."""
    from .pipeline import draft as run_draft
    from .pipeline.research import ResearchResult

    rr = ResearchResult(**research)
    envelope = run_draft(rr.run_id, rr, article_type=cfg.article_type)
    return {"run_id": rr.run_id, "envelope": envelope.model_dump(), "headline": envelope.headline}


@activity.defn(name="gate")
def gate_activity(run_id: int, envelope: dict) -> dict:
    """Score the draft with the gate judge; settle its spend. Returns quality_score."""
    import asyncio

    from .budget import estimate_cost_usd, settle
    from .eval import gate_judge_detailed
    from .pipeline.draft import ArticleEnvelope

    env = ArticleEnvelope.model_validate(envelope)
    claims_txt, spans = _load_run_claims(run_id)
    scores, result = gate_judge_detailed(env.body, claims_txt, spans)
    if result is not None:
        asyncio.run(
            settle(run_id, estimate_cost_usd(result.in_tokens, result.out_tokens, result.model))
        )
    activity.logger.info("gate: run_id=%s quality_score=%.3f", run_id, scores.weighted)
    return {"scores": scores.as_dict(), "quality_score": scores.weighted}


@activity.defn(name="escalation")
def escalation_activity(
    run_id: int, research: dict, envelope: dict, gate: dict, cfg: RunConfig
) -> dict:
    """Re-draft on the escalation model if the gate score is below threshold."""
    from .eval import RubricScores
    from .pipeline import escalate_if_needed
    from .pipeline.draft import ArticleEnvelope
    from .pipeline.research import ResearchResult

    rr = ResearchResult(**research)
    env = ArticleEnvelope.model_validate(envelope)
    gate_scores = RubricScores(**gate["scores"])
    claims_txt, spans = _load_run_claims(run_id)
    esc = escalate_if_needed(
        run_id, rr, env, gate_scores,
        claims=claims_txt, source_spans=spans, article_type=cfg.article_type,
    )
    activity.logger.info(
        "escalation: run_id=%s escalated=%s final=%.3f", run_id, esc.escalated, esc.final_score
    )
    return {
        "envelope": esc.envelope.model_dump(),
        "scores": esc.scores.as_dict(),
        "final_score": esc.final_score,
        "original_score": esc.original_score,
        "escalated": esc.escalated,
        "used_escalated": esc.used_escalated,
        "reason": esc.reason,
        "model_used": esc.model_used,
    }


@activity.defn(name="factcheck")
def factcheck_activity(run_id: int, envelope: dict) -> dict:
    """Run the local fact gate over the run's claims against their locked spans."""
    from .pipeline import fact_check
    from .pipeline.draft import ArticleEnvelope

    env = ArticleEnvelope.model_validate(envelope)
    fc = fact_check(run_id, env)
    activity.logger.info(
        "factcheck: run_id=%s passed=%s rate=%.0f%%", run_id, fc.passed, fc.pass_rate * 100
    )
    return {
        "passed": fc.passed,
        "pass_rate": fc.pass_rate,
        "total": fc.total,
        "num_passed": fc.num_passed,
        "meets_staging": fc.meets_staging,
    }


@activity.defn(name="humanize")
def humanize_activity(run_id: int, envelope: dict, cfg: RunConfig) -> dict:
    """Optional verified humanizer; settles its spend. Drift ⇒ body_final stays None."""
    from .config import settings

    if not (cfg.humanize or settings.humanize_enabled):
        return {"body_final": None, "ran": False, "passed": None, "reason": "humanize disabled"}

    import asyncio

    from sqlalchemy import select

    from .budget import estimate_cost_usd, settle
    from .db import get_sync_session_factory
    from .models import Claim
    from .pipeline import humanize_detailed, verify_humanize
    from .pipeline.draft import ArticleEnvelope

    env = ArticleEnvelope.model_validate(envelope)
    factory = get_sync_session_factory()
    with factory() as session:
        claims = list(
            session.execute(
                select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)
            ).scalars().all()
        )

    humanized_text, hum_result = humanize_detailed(env.body, cfg.article_type)
    if hum_result is not None:
        asyncio.run(
            settle(run_id, estimate_cost_usd(hum_result.in_tokens, hum_result.out_tokens, hum_result.model))
        )
    verify = verify_humanize(env.body, humanized_text, claims)
    body_final = humanized_text if verify.passed else None
    activity.logger.info(
        "humanize: run_id=%s passed=%s (%s)", run_id, verify.passed, verify.reason
    )
    return {"body_final": body_final, "ran": True, **verify.as_dict()}


@activity.defn(name="persist")
def persist_activity(payload: dict) -> int:
    """Persist the article + gate eval + humanize verdict + embedding. Idempotent."""
    from sqlalchemy import select

    from .db import get_sync_session_factory
    from .eval import RubricScores, store_eval
    from .models import Article
    from .pipeline import persist_article
    from .pipeline.draft import ArticleEnvelope

    run_id = payload["run_id"]
    factory = get_sync_session_factory()
    with factory() as session:
        existing = session.execute(
            select(Article).where(Article.run_id == run_id)
        ).scalar_one_or_none()
        if existing is not None:
            activity.logger.info("persist: article exists for run %s (id=%s)", run_id, existing.id)
            return existing.id

    env = ArticleEnvelope.model_validate(payload["envelope"])
    article_id = persist_article(
        run_id, env,
        slug_suffix=payload.get("source_id"),
        fact_pass_rate=payload.get("fact_pass_rate"),
        quality_score=payload.get("quality_score"),
        body_final_md=payload.get("body_final"),
        status=payload.get("status", "drafted"),
    )

    gate_scores = payload.get("gate_scores")
    if gate_scores:
        scores = RubricScores(**gate_scores)
        scores.judge_kind = "gate"
        store_eval(article_id, scores)

    hum = payload.get("humanize") or {}
    if hum.get("ran"):
        from .pipeline import record_humanize
        from .pipeline.humanize import HumanizeVerifyResult

        record_humanize(
            article_id,
            HumanizeVerifyResult(
                passed=bool(hum.get("passed")),
                drift_detected=bool(hum.get("drift_detected")),
                drift_details=hum.get("drift_details") or {},
                fact_pass_rate=float(hum.get("fact_pass_rate", 1.0)),
                citation_intact=bool(hum.get("citation_intact", True)),
                reason=hum.get("reason", ""),
            ),
        )

    try:
        from .embedding import embed_article

        embed_article(article_id)
    except Exception as exc:  # noqa: BLE001 — embedding is best-effort
        activity.logger.warning("persist: embed skipped: %s: %s", type(exc).__name__, exc)

    activity.logger.info("persist: run_id=%s article_id=%s status=%s", run_id, article_id, payload.get("status"))
    return article_id


@activity.defn(name="publish")
def publish_activity(run_id: int, envelope: dict) -> dict:
    """Render the fact-gated article into the Astro content collection (idempotent)."""
    from .pipeline import publish as run_publish
    from .pipeline.draft import ArticleEnvelope

    env = ArticleEnvelope.model_validate(envelope)
    result = run_publish(run_id, env)
    activity.logger.info("publish: run_id=%s slug=%s status=%s", run_id, result.slug, result.status)
    return {
        "slug": result.slug,
        "status": result.status,
        "file_path": result.file_path,
        "article_id": result.article_id,
        "already_published": result.already_published,
    }


@activity.defn(name="distribute")
def distribute_activity(article_id: int) -> dict:
    """Repackage a published article into X + Telegram payloads. Best-effort."""
    import asyncio

    from .budget import kill_switch_active
    from .config import settings

    if not settings.distribution_enabled:
        return {"distributed": False, "reason": "disabled"}
    if asyncio.run(kill_switch_active()):
        activity.logger.warning("distribute skipped: kill-switch is ON")
        return {"distributed": False, "reason": "kill-switch ON"}
    try:
        from .distribute import distribute_article

        result = distribute_article(article_id, ("x", "telegram"))
        activity.logger.info(
            "distribute: article_id=%s ids=%s skipped=%s",
            article_id, result.distribution_ids, result.skipped,
        )
        return {"distributed": True, "distribution_ids": result.distribution_ids}
    except Exception as exc:  # noqa: BLE001 — distribution must never fail a run
        activity.logger.warning("distribute skipped: %s: %s", type(exc).__name__, exc)
        return {"distributed": False, "reason": f"{type(exc).__name__}: {exc}"}


@activity.defn(name="settle")
def settle_activity(run_id: int, research: dict) -> dict:
    """Reconcile research-stage spend against the reservation."""
    import asyncio

    from .budget import estimate_cost_usd, settle
    from .config import settings

    cost = estimate_cost_usd(
        research.get("in_tokens", 0), research.get("out_tokens", 0), settings.model_primary
    )
    asyncio.run(settle(run_id, cost))
    return {"settled_usd": cost}


@activity.defn(name="mark_dlq")
def mark_dlq_activity(run_id: int, reason: str) -> None:
    """Route a run to the dead-letter sink (``runs.stage='dlq'``)."""
    _set_stage(run_id, "dlq", error=reason[:500])
    activity.logger.warning("mark_dlq: run_id=%s reason=%s", run_id, reason[:200])


# All activities, for the worker registration list.
ACTIVITIES = [
    start_run_activity,
    research_activity,
    draft_activity,
    gate_activity,
    escalation_activity,
    factcheck_activity,
    humanize_activity,
    persist_activity,
    publish_activity,
    distribute_activity,
    settle_activity,
    mark_dlq_activity,
]


# --- workflow ----------------------------------------------------------------

@workflow.defn
class PipelineWorkflow:
    """Chains the pipeline stages as activities (plan §3.4 state machine → workflow)."""

    @workflow.run
    async def run(self, cfg: RunConfig) -> dict:
        summary: dict = {
            "source_id": cfg.source_id,
            "article_type": cfg.article_type,
        }

        # 1. start: kill-switch + budget reservation + create the run row.
        run_id = await workflow.execute_activity(
            start_run_activity, cfg,
            start_to_close_timeout=_IO_TIMEOUT, retry_policy=_IO_RETRY,
        )
        summary["run_id"] = run_id

        # 2. research — DLQ on failure after retries.
        try:
            research = await workflow.execute_activity(
                research_activity, args=[run_id, cfg],
                start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY,
            )
        except ActivityError as exc:
            await workflow.execute_activity(
                mark_dlq_activity, args=[run_id, f"research failed: {exc.cause}"],
                start_to_close_timeout=_IO_TIMEOUT, retry_policy=_IO_RETRY,
            )
            return {**summary, "status": "dlq", "failed_stage": "research", "error": str(exc.cause)}

        # 3. draft.
        draft_out = await workflow.execute_activity(
            draft_activity, args=[research, cfg],
            start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY,
        )
        envelope = draft_out["envelope"]
        summary["headline"] = draft_out["headline"]

        # 4. gate judge.
        gate = await workflow.execute_activity(
            gate_activity, args=[run_id, envelope],
            start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY,
        )

        # 5. escalation (re-draft below threshold; picks the better draft).
        esc = await workflow.execute_activity(
            escalation_activity, args=[run_id, research, envelope, gate, cfg],
            start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY,
        )
        envelope = esc["envelope"]
        quality_score = esc["final_score"]

        # 6. fact gate.
        fc = await workflow.execute_activity(
            factcheck_activity, args=[run_id, envelope],
            start_to_close_timeout=_IO_TIMEOUT, retry_policy=_IO_RETRY,
        )

        # 7. humanize (optional; verified — drift falls back to the draft).
        hum = await workflow.execute_activity(
            humanize_activity, args=[run_id, envelope, cfg],
            start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY,
        )

        status = "fact_checked" if fc["passed"] else "drafted"

        # 8. persist (+ gate eval + humanize verdict + embedding).
        article_id = await workflow.execute_activity(
            persist_activity,
            {
                "run_id": run_id,
                "envelope": envelope,
                "quality_score": quality_score,
                "fact_pass_rate": fc["pass_rate"],
                "status": status,
                "source_id": cfg.source_id,
                "gate_scores": esc["scores"],
                "humanize": hum,
                "body_final": hum.get("body_final"),
            },
            start_to_close_timeout=_PERSIST_TIMEOUT, retry_policy=_IO_RETRY,
        )

        # 9. publish (optional, only when the fact gate passed; idempotent).
        published = None
        if cfg.publish and status == "fact_checked":
            published = await workflow.execute_activity(
                publish_activity, args=[run_id, envelope],
                start_to_close_timeout=_IO_TIMEOUT, retry_policy=_IO_RETRY,
            )

        # 10. reconcile research spend against the reservation FIRST, so a failed
        #     (or timed-out) best-effort distribute can never skip settlement and
        #     leak the reservation into budget_day.reserved_usd (C1).
        await workflow.execute_activity(
            settle_activity, args=[run_id, research],
            start_to_close_timeout=_IO_TIMEOUT, retry_policy=_IO_RETRY,
        )

        # 11. distribute (best-effort; only when actually published). A timeout or
        #     worker crash surfaces as an ActivityError at this call site — swallow
        #     it (log only) so distribution never fails an already-published run (C1).
        if published and not published.get("already_published"):
            try:
                await workflow.execute_activity(
                    distribute_activity, args=[published["article_id"]],
                    start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_IO_RETRY,
                )
            except ActivityError as exc:
                workflow.logger.warning(
                    "distribute failed (best-effort, ignored): %s", exc.cause
                )

        return {
            **summary,
            "article_id": article_id,
            "status": "published" if published else status,
            "quality_score": quality_score,
            "escalated": esc["escalated"],
            "used_escalated": esc["used_escalated"],
            "fact_passed": fc["passed"],
            "fact_pass_rate": fc["pass_rate"],
            "humanized": bool(hum.get("body_final")),
            "published_slug": published["slug"] if published else None,
        }
