"""Single-view newsroom dashboard aggregation (plan §6 Phase 2).

Pulls a snapshot of the whole pipeline from the existing tables — today's
output, the budget + kill-switch, eval agreement, the review-queue depth, and
source health — into one :class:`DashboardData`. The CLI (``newsroom
dashboard``) renders it; keeping the gathering here keeps ``cli.py`` thin and
the queries testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select

from .budget import budget_status
from .db import async_session_factory
from .eval import eval_stats
from .models import Article
from .sources.health import (
    STATUS_GREEN,
    STATUS_IDLE,
    STATUS_NEW,
    STATUS_RED,
    STATUS_YELLOW,
    SourceHealthStatus,
    check_source_health,
)


@dataclass(slots=True)
class TodayMetrics:
    """Counts + quality averages for articles touched/published today."""

    published: int = 0
    touched: int = 0
    fact_pass_rate: float | None = None
    quality_avg: float | None = None


@dataclass(slots=True)
class DashboardData:
    """Everything the dashboard view needs, gathered in one pass."""

    day: str
    today: TodayMetrics
    budget: dict
    evals: dict
    queue_depth: int
    health: dict[str, SourceHealthStatus] = field(default_factory=dict)
    health_counts: dict[str, int] = field(default_factory=dict)


async def _today_and_queue(now: datetime) -> tuple[TodayMetrics, int]:
    """Today's article metrics + the current review-queue depth (one session)."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session_factory() as session:
        published = (
            await session.execute(
                select(func.count())
                .select_from(Article)
                .where(Article.status == "published", Article.published_at >= today_start)
            )
        ).scalar() or 0
        touched = (
            await session.execute(
                select(func.count())
                .select_from(Article)
                .where(Article.updated_at >= today_start)
            )
        ).scalar() or 0
        fpr = (
            await session.execute(
                select(func.avg(Article.fact_pass_rate)).where(
                    Article.updated_at >= today_start
                )
            )
        ).scalar()
        quality = (
            await session.execute(
                select(func.avg(Article.quality_score)).where(
                    Article.updated_at >= today_start
                )
            )
        ).scalar()
        queue_depth = (
            await session.execute(
                select(func.count())
                .select_from(Article)
                .where(Article.review_path == "queued")
            )
        ).scalar() or 0

    metrics = TodayMetrics(
        published=int(published),
        touched=int(touched),
        fact_pass_rate=float(fpr) if fpr is not None else None,
        quality_avg=float(quality) if quality is not None else None,
    )
    return metrics, int(queue_depth)


def _summarise_health(health: dict[str, SourceHealthStatus]) -> dict[str, int]:
    counts = {
        STATUS_GREEN: 0,
        STATUS_YELLOW: 0,
        STATUS_RED: 0,
        STATUS_NEW: 0,
        STATUS_IDLE: 0,
    }
    for status in health.values():
        counts[status.status] = counts.get(status.status, 0) + 1
    return counts


async def gather_dashboard(*, now: datetime | None = None) -> DashboardData:
    """Assemble the full dashboard snapshot from the live tables."""
    now = now or datetime.now(timezone.utc)
    today, queue_depth = await _today_and_queue(now)
    budget = await budget_status()
    health = await check_source_health(now=now)
    # eval_stats is synchronous (sync session); calling it inline is fine for a
    # one-shot CLI command.
    evals = eval_stats()
    return DashboardData(
        day=now.date().isoformat(),
        today=today,
        budget=budget,
        evals=evals,
        queue_depth=queue_depth,
        health=health,
        health_counts=_summarise_health(health),
    )
