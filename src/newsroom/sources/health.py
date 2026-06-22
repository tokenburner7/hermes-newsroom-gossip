"""Source-health monitor (plan O-M2, Phase 2 §6).

Silent feed rot — a source that quietly stops returning items — narrows the
corpus before anyone notices. :func:`check_source_health` compares each source
class's ingestion volume *so far today* against the average volume over the
*same elapsed slice* of each of the prior ``N`` days, and flags a class whose
volume has dropped more than ``source_health_drop_threshold`` (default 50%).

Comparing the same elapsed slice (midnight→now vs midnight→now on past days)
rather than whole days avoids a false alarm every morning when today is only
partially complete. Each check writes an ``source_health`` row keyed by
``(source_class, window_start)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import settings
from ..db import async_session_factory
from ..models import Source, SourceHealth

log = logging.getLogger(__name__)

# Health states.
STATUS_GREEN = "green"      # volume at/above baseline (or only a mild dip)
STATUS_YELLOW = "yellow"    # noticeable dip (>= half the alert threshold)
STATUS_RED = "red"          # drop past the alert threshold — likely feed rot
STATUS_NEW = "new"          # items today but no baseline yet (fresh source)
STATUS_IDLE = "idle"        # no items today and no baseline (nothing to judge)


@dataclass(slots=True)
class SourceHealthStatus:
    """Per-source health snapshot (plan §6 ``check_source_health`` return)."""

    source_class: str
    status: str
    items_today: int
    baseline_avg: float
    drop_pct: float

    @property
    def is_alert(self) -> bool:
        return self.status == STATUS_RED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "items_today": self.items_today,
            "baseline_avg": round(self.baseline_avg, 2),
            "drop_pct": round(self.drop_pct, 4),
        }


def classify_health(
    items_today: int, baseline_avg: float, *, threshold: float | None = None
) -> tuple[str, float]:
    """Pure rule mapping (today, baseline) -> (status, drop_pct).

    ``drop_pct`` is clamped to ``[0, 1]`` — a *surplus* over baseline is not a
    drop. ``red`` at/above the threshold, ``yellow`` at/above half of it.
    """
    threshold = (
        settings.source_health_drop_threshold if threshold is None else threshold
    )
    if baseline_avg <= 0:
        return (STATUS_NEW if items_today > 0 else STATUS_IDLE), 0.0
    drop = max(0.0, (baseline_avg - items_today) / baseline_avg)
    if drop >= threshold:
        status = STATUS_RED
    elif drop >= threshold / 2:
        status = STATUS_YELLOW
    else:
        status = STATUS_GREEN
    return status, drop


def _known_source_classes() -> set[str]:
    """The registered source class names (imported lazily to avoid import cost)."""
    from . import SOURCES

    return set(SOURCES.keys())


async def _write_health(results: dict[str, SourceHealthStatus], window_start: datetime) -> None:
    """Upsert one ``source_health`` row per class for this check's window."""
    if not results:
        return
    rows = [
        {
            "source_class": s.source_class,
            "window_start": window_start,
            "items_seen": s.items_today,
            "errors": 0,
        }
        for s in results.values()
    ]
    stmt = pg_insert(SourceHealth).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_class", "window_start"],
        set_={"items_seen": stmt.excluded.items_seen, "errors": stmt.excluded.errors},
    )
    async with async_session_factory() as session:
        await session.execute(stmt)
        await session.commit()


async def check_source_health(
    *, now: datetime | None = None, write: bool = True
) -> dict[str, SourceHealthStatus]:
    """Compute per-source ingestion health and (optionally) persist it.

    Returns ``{source_class: SourceHealthStatus}`` covering every class seen in
    the lookback window *and* every registered source (so a fully-dead feed with
    a prior baseline still surfaces as ``red``).
    """
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - today_start
    baseline_days = max(1, settings.source_health_baseline_days)
    window_lo = today_start - timedelta(days=baseline_days)

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Source.source_class, Source.retrieved_at).where(
                    Source.retrieved_at >= window_lo
                )
            )
        ).all()

    by_class: dict[str, list[datetime]] = {}
    for source_class, retrieved_at in rows:
        if retrieved_at is None:
            continue
        by_class.setdefault(source_class, []).append(retrieved_at)

    classes = set(by_class) | _known_source_classes()
    results: dict[str, SourceHealthStatus] = {}
    for source_class in sorted(classes):
        timestamps = by_class.get(source_class, [])
        items_today = sum(1 for ts in timestamps if today_start <= ts <= now)
        # Same elapsed slice of each prior day.
        baseline_counts = [
            sum(
                1
                for ts in timestamps
                if (today_start - timedelta(days=d))
                <= ts
                < (today_start - timedelta(days=d) + elapsed)
            )
            for d in range(1, baseline_days + 1)
        ]
        baseline_avg = sum(baseline_counts) / len(baseline_counts) if baseline_counts else 0.0
        status, drop = classify_health(items_today, baseline_avg)
        results[source_class] = SourceHealthStatus(
            source_class=source_class,
            status=status,
            items_today=items_today,
            baseline_avg=baseline_avg,
            drop_pct=drop,
        )

    if write:
        await _write_health(results, today_start)
    alerts = [s.source_class for s in results.values() if s.is_alert]
    if alerts:
        log.warning("source-health ALERT (>%.0f%% drop): %s",
                    settings.source_health_drop_threshold * 100, ", ".join(alerts))
    return results
