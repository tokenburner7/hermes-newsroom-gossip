"""Process-local circuit breakers (plan §6, Phase 2A).

A single small breaker implementation shared by two call sites:

* :mod:`newsroom.ingest` — one breaker per *source* (arXiv, SEC, …) so a feed that
  is down or rate-limiting us stops being hammered every poll.
* :mod:`newsroom.llm.client` — one breaker per *provider* (DeepSeek, OpenRouter)
  so failover stops re-trying a provider that is returning 429/5xx in a tight loop.

State machine (classic three-state breaker)::

    CLOSED  --(fail_threshold consecutive failures)-->  OPEN
    OPEN    --(backoff elapsed)-->                       HALF_OPEN   (one probe allowed)
    HALF_OPEN --(probe succeeds)-->                      CLOSED      (backoff reset)
    HALF_OPEN --(probe fails)-->                         OPEN        (backoff doubled, capped)

The backoff starts at ``recovery_s`` (default 30s) and doubles on every failed
half-open probe up to ``max_backoff_s`` (default 5 min), so a persistently dead
dependency is probed less and less often. A success anywhere resets everything.

State is **per-process** and lost on restart — deliberately fine for Phase 2
(plan §6). No persistence, no cross-worker coordination. A :class:`threading.Lock`
guards each breaker so it is safe under the asyncio ingest fan-out and the sync
LLM client alike.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .config import settings

log = logging.getLogger(__name__)


class BreakerState(str, Enum):
    """The three breaker states (str-valued so they serialise cleanly in reports)."""

    CLOSED = "closed"          # healthy: calls pass through
    OPEN = "open"              # tripped: calls are refused until the backoff elapses
    HALF_OPEN = "half_open"    # probing: a single trial call is allowed through


class CircuitOpenError(RuntimeError):
    """Raised by :meth:`CircuitBreaker.guard` when the breaker is OPEN."""

    def __init__(self, name: str, retry_after_s: float) -> None:
        self.name = name
        self.retry_after_s = retry_after_s
        super().__init__(
            f"circuit breaker {name!r} is OPEN (retry in ~{retry_after_s:.0f}s)"
        )


@dataclass
class CircuitBreaker:
    """A single named breaker. Thread-safe; all transitions go through the lock."""

    name: str
    fail_threshold: int = 5
    recovery_s: float = 30.0
    max_backoff_s: float = 300.0

    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    backoff_s: float = field(default=0.0)
    # Lifetime counters, handy for the ingest-all / status reports.
    total_failures: int = 0
    total_successes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.backoff_s <= 0:
            self.backoff_s = self.recovery_s

    # -- decision -----------------------------------------------------------

    def allow(self, *, now: float | None = None) -> bool:
        """Return True if a call may proceed, advancing OPEN→HALF_OPEN when due.

        This mutates state (an elapsed OPEN window flips to HALF_OPEN and lets a
        single probe through), so call it exactly once per attempt.
        """
        clock = time.monotonic() if now is None else now
        with self._lock:
            if self.state is BreakerState.CLOSED:
                return True
            if self.state is BreakerState.OPEN:
                if self.opened_at is not None and (clock - self.opened_at) >= self.backoff_s:
                    self.state = BreakerState.HALF_OPEN
                    log.info("circuit %s: OPEN → HALF_OPEN (probe)", self.name)
                    return True
                return False
            # HALF_OPEN: let the probe through (single-process; at most a few in flight).
            return True

    def allow_request(self, *, now: float | None = None) -> bool:
        """Spec alias for :meth:`allow` — True if a request may proceed (plan §6 API)."""
        return self.allow(now=now)

    def guard(self, *, now: float | None = None) -> None:
        """Like :meth:`allow` but raises :class:`CircuitOpenError` when refused."""
        if not self.allow(now=now):
            raise CircuitOpenError(self.name, self.retry_after_s())

    # -- outcome reporting --------------------------------------------------

    def record_success(self) -> None:
        """Reset to CLOSED. A healthy call clears the failure streak and backoff."""
        with self._lock:
            self.total_successes += 1
            was = self.state
            self.consecutive_failures = 0
            self.state = BreakerState.CLOSED
            self.opened_at = None
            self.backoff_s = self.recovery_s
        if was is not BreakerState.CLOSED:
            log.info("circuit %s: %s → CLOSED (recovered)", self.name, was.value)

    def record_failure(self, *, now: float | None = None) -> None:
        """Count a failure; trip OPEN at the threshold, or re-open a failed probe."""
        clock = time.monotonic() if now is None else now
        with self._lock:
            self.total_failures += 1
            self.consecutive_failures += 1
            if self.state is BreakerState.HALF_OPEN:
                # Probe failed: re-open with doubled (capped) backoff.
                self.backoff_s = min(self.backoff_s * 2, self.max_backoff_s)
                self.state = BreakerState.OPEN
                self.opened_at = clock
                log.warning(
                    "circuit %s: HALF_OPEN probe failed → OPEN (backoff %.0fs)",
                    self.name, self.backoff_s,
                )
            elif self.state is BreakerState.CLOSED:
                if self.consecutive_failures >= self.fail_threshold:
                    self.backoff_s = self.recovery_s
                    self.state = BreakerState.OPEN
                    self.opened_at = clock
                    log.warning(
                        "circuit %s: %d consecutive failures → OPEN (backoff %.0fs)",
                        self.name, self.consecutive_failures, self.backoff_s,
                    )
            else:  # already OPEN (a straggler in-flight call failed); refresh the window
                self.opened_at = clock

    # -- introspection ------------------------------------------------------

    def retry_after_s(self, *, now: float | None = None) -> float:
        """Seconds until the next probe is allowed (0 when not OPEN)."""
        clock = time.monotonic() if now is None else now
        with self._lock:
            if self.state is not BreakerState.OPEN or self.opened_at is None:
                return 0.0
            return max(0.0, self.backoff_s - (clock - self.opened_at))

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    def snapshot(self) -> dict:
        """A JSON-friendly view of the breaker for status output / logging."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "consecutive_failures": self.consecutive_failures,
                "backoff_s": round(self.backoff_s, 1),
                "retry_after_s": round(self.retry_after_s(), 1),
                "total_failures": self.total_failures,
                "total_successes": self.total_successes,
            }


class CircuitBreakerRegistry:
    """A lazily-populated, thread-safe map of name → :class:`CircuitBreaker`.

    All breakers in a registry share the same thresholds (sourced from
    :mod:`newsroom.config` by default). Used as a module-level singleton at each
    call site (one for ingest sources, one for LLM providers).
    """

    def __init__(
        self,
        *,
        fail_threshold: int | None = None,
        recovery_s: float | None = None,
        max_backoff_s: float | None = None,
    ) -> None:
        self.fail_threshold = (
            settings.circuit_breaker_fail_threshold
            if fail_threshold is None else fail_threshold
        )
        self.recovery_s = (
            settings.circuit_breaker_recovery_s if recovery_s is None else recovery_s
        )
        self.max_backoff_s = (
            settings.circuit_breaker_max_backoff_s
            if max_backoff_s is None else max_backoff_s
        )
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        """Return the breaker for ``name``, creating it on first use."""
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                breaker = CircuitBreaker(
                    name=name,
                    fail_threshold=self.fail_threshold,
                    recovery_s=self.recovery_s,
                    max_backoff_s=self.max_backoff_s,
                )
                self._breakers[name] = breaker
            return breaker

    def snapshot(self) -> dict[str, dict]:
        """Snapshot of every breaker that has been touched this process."""
        with self._lock:
            return {name: b.snapshot() for name, b in self._breakers.items()}

    def reset(self) -> None:
        """Drop all breakers (test helper)."""
        with self._lock:
            self._breakers.clear()
