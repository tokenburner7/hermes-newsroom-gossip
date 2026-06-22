"""Distributional drift monitor (plan O-C2, Phase 2 §6).

A per-article gate score staying ≥0.80 does not prove the newsroom is healthy:
the *distribution* of quality can quietly shift while every point still clears
the bar. :func:`compute_drift` runs a two-sample Kolmogorov–Smirnov test of the
trailing-``N``-day gate-score distribution against the prior ``N``-day baseline
and flags a significant shift.

Scores come from the ``evals`` table (``judge_kind = 'gate'``, the per-article
quality gate written by :mod:`newsroom.eval.runner`). Drift is declared when
``p < drift_p_value_threshold`` OR ``KS > drift_ks_threshold``. The result is
itself persisted as a ``judge_kind = 'drift'`` row in ``evals`` so the monitor
keeps its own audit trail.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings
from ..db import get_sync_session_factory
from ..models import Eval

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DriftResult:
    """Outcome of one drift check (plan §6 ``compute_drift`` return)."""

    drift_detected: bool = False
    ks_statistic: float | None = None
    p_value: float | None = None
    current_median: float | None = None
    baseline_median: float | None = None
    n_current: int = 0
    n_baseline: int = 0
    # True when a window had too few scores for a meaningful test.
    insufficient: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        """JSON-serialisable payload (used for the ``evals.scores_json`` audit row)."""
        return {
            "drift_detected": self.drift_detected,
            "ks_statistic": self.ks_statistic,
            "p_value": self.p_value,
            "current_median": self.current_median,
            "baseline_median": self.baseline_median,
            "n_current": self.n_current,
            "n_baseline": self.n_baseline,
            "insufficient": self.insufficient,
            "note": self.note,
        }


def decide_drift(
    ks_statistic: float | None,
    p_value: float | None,
    *,
    ks_threshold: float | None = None,
    p_threshold: float | None = None,
) -> bool:
    """Pure rule: drift iff ``p < p_threshold`` OR ``KS > ks_threshold``."""
    ks_threshold = settings.drift_ks_threshold if ks_threshold is None else ks_threshold
    p_threshold = (
        settings.drift_p_value_threshold if p_threshold is None else p_threshold
    )
    by_p = p_value is not None and p_value < p_threshold
    by_ks = ks_statistic is not None and ks_statistic > ks_threshold
    return bool(by_p or by_ks)


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _load_gate_scores(session, *, lo: datetime, hi: datetime) -> list[float]:
    """Gate-judge weighted scores recorded in ``[lo, hi)``."""
    rows = session.execute(
        select(Eval.weighted).where(
            Eval.judge_kind == "gate",
            Eval.weighted.isnot(None),
            Eval.ts >= lo,
            Eval.ts < hi,
        )
    ).scalars().all()
    return [float(x) for x in rows]


def _store_drift(result: DriftResult) -> int | None:
    """Persist the drift result as a ``judge_kind='drift'`` row in ``evals``."""
    factory = get_sync_session_factory()
    with factory() as session:
        row = Eval(
            article_id=None,
            judge_kind="drift",
            judge_model="scipy.ks_2samp",
            scores_json=result.to_dict(),
            weighted=result.ks_statistic,
        )
        session.add(row)
        session.commit()
        return row.id


def evaluate_distributions(
    baseline: list[float],
    current: list[float],
    *,
    min_samples: int | None = None,
) -> DriftResult:
    """Pure KS comparison of two score lists (no DB, no persistence).

    Returns ``insufficient`` when either window has fewer than ``min_samples``
    scores; otherwise runs ``scipy.stats.ks_2samp`` and applies
    :func:`decide_drift`. Kept separate from :func:`compute_drift` so the
    statistics are testable without a database.
    """
    min_n = max(1, settings.drift_min_samples if min_samples is None else min_samples)
    if len(current) < min_n or len(baseline) < min_n:
        return DriftResult(
            insufficient=True,
            n_current=len(current),
            n_baseline=len(baseline),
            current_median=_median(current),
            baseline_median=_median(baseline),
            note=(
                f"insufficient samples (need ≥{min_n} per window; have "
                f"{len(baseline)} baseline / {len(current)} current)"
            ),
        )

    # Imported lazily so importing the module never pulls in SciPy.
    from scipy.stats import ks_2samp

    ks_statistic, p_value = ks_2samp(baseline, current)
    detected = decide_drift(float(ks_statistic), float(p_value))
    return DriftResult(
        drift_detected=detected,
        ks_statistic=float(ks_statistic),
        p_value=float(p_value),
        current_median=_median(current),
        baseline_median=_median(baseline),
        n_current=len(current),
        n_baseline=len(baseline),
        note=(
            "DRIFT: gate-score distribution shifted vs the prior baseline"
            if detected
            else "stable: no significant shift vs the prior baseline"
        ),
    )


def compute_drift(
    *,
    now: datetime | None = None,
    window_days: int | None = None,
    store: bool = True,
) -> DriftResult:
    """KS-test the trailing-window gate scores vs the prior-window baseline.

    * current window  = ``[now - window_days, now)``
    * baseline window = ``[now - 2*window_days, now - window_days)``

    Returns a :class:`DriftResult`. When either window has fewer than
    ``drift_min_samples`` scores the test is skipped (``insufficient=True``) and
    nothing is stored. Otherwise the result is persisted to ``evals`` when
    ``store`` is true.
    """
    now = now or datetime.now(timezone.utc)
    window_days = window_days or settings.drift_window_days
    current_lo = now - timedelta(days=window_days)
    baseline_lo = now - timedelta(days=2 * window_days)

    factory = get_sync_session_factory()
    with factory() as session:
        current = _load_gate_scores(session, lo=current_lo, hi=now)
        baseline = _load_gate_scores(session, lo=baseline_lo, hi=current_lo)

    result = evaluate_distributions(baseline, current)
    if result.insufficient:
        return result
    if store:
        _store_drift(result)
    log.info(
        "drift check: detected=%s ks=%.3f p=%.4f (baseline n=%d med=%s, current n=%d med=%s)",
        result.drift_detected, result.ks_statistic, result.p_value,
        result.n_baseline, result.baseline_median, result.n_current, result.current_median,
    )
    return result
