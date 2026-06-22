"""Multi-source ingestion orchestrator + per-source circuit breakers (plan §6).

Runs the registered :data:`newsroom.sources.SOURCES` and aggregates their
``(fetched, upserted)`` counts. Sources are independent third-party services, so
:func:`ingest_all` runs them concurrently; a failure in one is logged and
recorded as ``(0, 0)`` rather than aborting the batch.

Phase 2A adds a **per-source circuit breaker** (:mod:`newsroom.circuit_breaker`):
a feed that fails ``circuit_breaker_fail_threshold`` polls in a row trips OPEN and
is skipped (raising :class:`CircuitOpenError`) until a half-open probe is due, so a
dead or rate-limiting source stops being hammered. Breaker state is per-process
(lost on restart — fine for Phase 2) and surfaced via :func:`breaker_snapshot`,
which the ``newsroom ingest-all`` CLI renders alongside the counts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .circuit_breaker import CircuitBreakerRegistry, CircuitOpenError
from .sources import SOURCES

log = logging.getLogger(__name__)

# arXiv's ingest() predates the registry and requires a look-back window; this
# is the default the orchestrator passes when running it as part of a batch.
ARXIV_DEFAULT_SINCE = timedelta(days=1)

# One breaker per source name, sharing the config-driven thresholds. Module-level
# so state persists across polls within a long-running process (e.g. the worker).
source_breakers = CircuitBreakerRegistry()


async def ingest_source(name: str) -> tuple[int, int]:
    """Run a single named source through its breaker. Returns ``(fetched, upserted)``.

    Raises ``KeyError`` for an unknown source name, or :class:`CircuitOpenError`
    if the source's breaker is OPEN (skip this poll). A raised ingestion error is
    recorded as a breaker failure and re-raised; a success resets the breaker.
    """
    module = SOURCES[name]
    breaker = source_breakers.get(name)
    breaker.guard()  # CircuitOpenError if OPEN — skip without touching the source
    try:
        if name == "arxiv":
            # arXiv keeps its richer signature; supply the batch default window.
            result = await module.ingest(ARXIV_DEFAULT_SINCE)
        else:
            result = await module.ingest()
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    return result


async def ingest_all() -> dict[str, tuple[int, int]]:
    """Run every registered source concurrently. Returns per-source counts.

    Each source's result is its ``(fetched, upserted)`` pair; sources that raise
    (including an OPEN breaker) are logged and reported as ``(0, 0)`` so one bad
    source never sinks the run. Inspect :func:`breaker_snapshot` for breaker state.
    """
    names = list(SOURCES)
    results = await asyncio.gather(
        *(ingest_source(name) for name in names), return_exceptions=True
    )
    counts: dict[str, tuple[int, int]] = {}
    for name, result in zip(names, results):
        if isinstance(result, CircuitOpenError):
            log.warning("source %s skipped: %s", name, result)
            counts[name] = (0, 0)
        elif isinstance(result, Exception):
            log.warning("source %s failed: %s", name, result)
            counts[name] = (0, 0)
        else:
            counts[name] = result
    return counts


def breaker_snapshot() -> dict[str, dict]:
    """Per-source breaker state (name → snapshot) touched so far this process."""
    return source_breakers.snapshot()
