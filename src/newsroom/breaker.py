"""Public ``breaker`` alias for the circuit-breaker implementation (plan §6, Phase 2A).

The breaker logic lives in :mod:`newsroom.circuit_breaker` — a single
implementation shared by two call sites (per-source ingest breakers and
per-provider LLM breakers). This module is the stable, short public name for
that machinery; it adds **no** logic of its own (KISS/DRY: one source of truth).

Import from here when you want the breaker API by its plan name::

    from newsroom.breaker import CircuitBreaker, CircuitBreakerRegistry

The :class:`CircuitBreaker` API matches the Phase-2A spec:

* ``CircuitBreaker(name, fail_threshold=5, recovery_s=30, max_backoff_s=300)``
* ``state`` — a :class:`BreakerState` (``"closed" | "open" | "half_open"``)
* ``record_success()`` / ``record_failure()`` — report a call's outcome
* ``allow_request() -> bool`` — whether a call may proceed (alias of ``allow``)

It is thread-safe (a :class:`threading.Lock` guards every transition), so the
same breaker is safe under the asyncio ingest fan-out and the sync LLM client.
"""

from __future__ import annotations

from .circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
)

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitOpenError",
]
