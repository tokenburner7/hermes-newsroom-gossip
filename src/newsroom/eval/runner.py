"""Eval orchestration: load article inputs, run the judges, persist, summarise.

This is the DB-facing seam over the pure judges in :mod:`newsroom.eval.judge`. It
loads an article's body + locked claims/spans, runs the gate and/or eval judge,
records the per-article gate score on ``articles.quality_score``, writes a row per
judge into the ``evals`` table, and reconciles the judge's token spend against the
daily budget. :func:`eval_stats` summarises gate-vs-eval-judge agreement.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select, text

from ..budget import estimate_cost_usd, settle
from ..config import settings
from ..db import get_sync_session_factory
from ..llm import ChatResult
from ..models import Article, Claim, Eval
from .judge import RubricScores, eval_judge_detailed, gate_judge_detailed

log = logging.getLogger(__name__)


@dataclass(slots=True)
class EvalInputs:
    """The judge-ready view of one article."""

    article_id: int
    run_id: int | None
    article_text: str
    claims: list[str]
    source_spans: list[str]


# --- loading -----------------------------------------------------------------

def _load_article(session, *, article_id: int | None = None, run_id: int | None = None) -> Article | None:
    if article_id is not None:
        return session.get(Article, article_id)
    if run_id is not None:
        return session.execute(
            select(Article).where(Article.run_id == run_id).order_by(Article.id.desc())
        ).scalars().first()
    return None


def _load_claims(session, run_id: int | None, claim_ids: list[int] | None) -> list[Claim]:
    stmt = select(Claim)
    if claim_ids:
        stmt = stmt.where(Claim.id.in_(claim_ids))
    elif run_id is not None:
        stmt = stmt.where(Claim.run_id == run_id)
    else:
        return []
    return list(session.execute(stmt.order_by(Claim.id)).scalars().all())


def load_inputs(*, article_id: int | None = None, run_id: int | None = None) -> EvalInputs:
    """Assemble the judge inputs (body + claims + spans) for an article/run."""
    factory = get_sync_session_factory()
    with factory() as session:
        article = _load_article(session, article_id=article_id, run_id=run_id)
        if article is None:
            raise LookupError(
                f"no article found for article_id={article_id} run_id={run_id}"
            )
        claims = _load_claims(session, article.run_id, article.claims_used)
        return EvalInputs(
            article_id=article.id,
            run_id=article.run_id,
            article_text=article.body_final_md or article.body_md or "",
            claims=[c.claim_text for c in claims],
            source_spans=[c.supporting_span for c in claims],
        )


# --- persistence -------------------------------------------------------------

def store_eval(article_id: int, scores: RubricScores) -> int:
    """Insert one ``evals`` row from a :class:`RubricScores`; return its id."""
    factory = get_sync_session_factory()
    with factory() as session:
        row = Eval(
            article_id=article_id,
            judge_kind=scores.judge_kind or None,
            judge_model=scores.judge_model or None,
            scores_json=scores.scores_json(),
            weighted=float(scores.weighted),
        )
        session.add(row)
        session.commit()
        return row.id


def set_quality_score(article_id: int, score: float) -> None:
    """Persist the gate judge's weighted score on ``articles.quality_score``."""
    factory = get_sync_session_factory()
    with factory() as session:
        article = session.get(Article, article_id)
        if article is not None:
            article.quality_score = float(score)
            session.commit()


def _settle_judge_cost(run_id: int | None, result: ChatResult | None) -> float:
    """Reconcile a judge call's token spend against today's budget. Returns USD."""
    if result is None:
        return 0.0
    cost = estimate_cost_usd(result.in_tokens, result.out_tokens, result.model)
    try:
        asyncio.run(settle(run_id or 0, cost))
    except Exception as exc:  # noqa: BLE001 — budget settle is best-effort here
        log.warning("could not settle judge cost: %s", exc)
    return cost


# --- high-level operations ---------------------------------------------------

def run_gate_judge(*, article_id: int | None = None, run_id: int | None = None) -> RubricScores:
    """Run the gate judge for an article, store quality_score + an evals row."""
    inp = load_inputs(article_id=article_id, run_id=run_id)
    scores, result = gate_judge_detailed(inp.article_text, inp.claims, inp.source_spans)
    _settle_judge_cost(inp.run_id, result)
    set_quality_score(inp.article_id, scores.weighted)
    store_eval(inp.article_id, scores)
    return scores


def run_eval_judge(*, article_id: int | None = None, run_id: int | None = None) -> RubricScores:
    """Run the independent eval judge for an article and store an evals row."""
    inp = load_inputs(article_id=article_id, run_id=run_id)
    scores, result = eval_judge_detailed(inp.article_text, inp.claims, inp.source_spans)
    _settle_judge_cost(inp.run_id, result)
    store_eval(inp.article_id, scores)
    return scores


def evaluate(
    *, article_id: int | None = None, run_id: int | None = None, with_eval: bool = True
) -> dict:
    """Run both judges (gate always, eval optional) for an article and persist all.

    Returns a summary dict suitable for CLI rendering.
    """
    gate = run_gate_judge(article_id=article_id, run_id=run_id)
    out: dict = {"gate": gate}
    if with_eval:
        out["eval"] = run_eval_judge(article_id=article_id, run_id=run_id)
    return out


def should_sample_eval(run_id: int | None) -> bool:
    """True when this run is one of the sampled (every ``eval_sample_rate``-th) articles."""
    rate = max(1, int(settings.eval_sample_rate))
    return bool(run_id) and (int(run_id) % rate == 0)


# --- stats -------------------------------------------------------------------

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def eval_stats() -> dict:
    """Summarise gate-vs-eval-judge agreement across all recorded evals."""
    factory = get_sync_session_factory()
    with factory() as session:
        rows = session.execute(
            select(Eval.article_id, Eval.judge_kind, Eval.weighted, Eval.id)
            .where(Eval.weighted.isnot(None))
            .order_by(Eval.id)
        ).all()

    # Latest weighted score per (article_id, judge_kind).
    latest: dict[tuple[int, str], float] = {}
    counts: dict[str, int] = {}
    for article_id, kind, weighted, _id in rows:
        if article_id is None or kind is None or weighted is None:
            continue
        latest[(article_id, kind)] = float(weighted)
        counts[kind] = counts.get(kind, 0) + 1

    gate_vals = [v for (aid, k), v in latest.items() if k == "gate"]
    eval_vals = [v for (aid, k), v in latest.items() if k == "eval"]

    paired_ids = sorted(
        {aid for (aid, k) in latest if k == "gate"}
        & {aid for (aid, k) in latest if k == "eval"}
    )
    gate_paired = [latest[(aid, "gate")] for aid in paired_ids]
    eval_paired = [latest[(aid, "eval")] for aid in paired_ids]
    diffs = [abs(g - e) for g, e in zip(gate_paired, eval_paired)]

    def _mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    return {
        "total_evals": len(rows),
        "counts_by_kind": counts,
        "articles_with_gate": len(gate_vals),
        "articles_with_eval": len(eval_vals),
        "mean_gate": _mean(gate_vals),
        "mean_eval": _mean(eval_vals),
        "paired": len(paired_ids),
        "mean_abs_diff": _mean(diffs),
        "correlation": _pearson(gate_paired, eval_paired),
        "pairs": [
            {"article_id": aid, "gate": latest[(aid, "gate")], "eval": latest[(aid, "eval")]}
            for aid in paired_ids
        ],
    }
