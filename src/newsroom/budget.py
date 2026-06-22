"""Daily budget reservation, spend ledger, and the global kill-switch (O-C3).

Every LLM call must fit under a hard daily ceiling. The ceiling is enforced
*atomically* in the database by the ``reserve_budget(p_day, p_vertical, p_amount)`` SQL
function (see the initial migration): it bumps ``reserved_usd`` only if the new
total stays ``<= ceiling_usd`` and returns whether the reservation succeeded.
This module is the thin async Python seam over that function plus the
``system_state`` kill-switch.

Flow per run:
1. :func:`ensure_budget_day` — make sure today's ``budget_day`` row exists.
2. :func:`reserve` — try to reserve an estimate *before* spending; bail if False.
3. :func:`settle` — reconcile the actual cost after the call(s) finish.

All functions are ``async`` and use the async session factory. The kill-switch
(``system_state['kill_switch']``) lets an operator (or a guard) halt the pipeline
without a deploy: :func:`kill_switch_active` is checked before any run starts.

Every budget function accepts an optional ``vertical`` parameter (default
``"aixcrypto"``) so each content vertical has its own budget ceiling, escalation
counter, and kill-switch. The ``budget_day`` composite PK is ``(day, vertical)``.
"""

from __future__ import annotations

from datetime import date as _date

from sqlalchemy import text

from .config import settings
from .db import async_session_factory

# DeepSeek public pricing (USD per 1M tokens, cache-miss rates). Used to turn
# token counts into a dollar figure for :func:`settle`. Unknown models fall back
# to a conservative default so we never under-count spend.
_PRICING: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}
_DEFAULT_PRICE: tuple[float, float] = (0.30, 1.20)


def estimate_cost_usd(in_tokens: int, out_tokens: int, model: str = "") -> float:
    """Estimate USD cost for ``in_tokens``/``out_tokens`` on ``model``."""
    in_rate, out_rate = _PRICING.get(model, _DEFAULT_PRICE)
    return (in_tokens / 1_000_000) * in_rate + (out_tokens / 1_000_000) * out_rate


# --- budget_day --------------------------------------------------------------

async def ensure_budget_day(
    day: _date | None = None,
    ceiling: float | None = None,
    *,
    vertical: str = "gossip",
) -> None:
    """Create today's ``budget_day`` row if missing (idempotent).

    ``ceiling`` defaults to ``settings.daily_ceiling_usd`` and the escalation cap
    to ``settings.escalation_cap``. Existing rows are left untouched.
    """
    day = day or _date.today()
    ceiling = settings.daily_ceiling_usd if ceiling is None else ceiling
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO budget_day (day, vertical, ceiling_usd, escalation_cap)
                VALUES (:day, :vertical, :ceiling, :cap)
                ON CONFLICT (day, vertical) DO NOTHING
                """
            ),
            {"day": day, "vertical": vertical, "ceiling": ceiling, "cap": settings.escalation_cap},
        )
        await session.commit()


async def reserve(
    day: _date | None = None, est_usd: float = 0.001, *, vertical: str = "gossip"
) -> bool:
    """Atomically reserve ``est_usd`` against today's per-vertical ceiling.

    Delegates to the ``reserve_budget`` SQL function, which only bumps
    ``reserved_usd`` if it stays ``<= ceiling_usd``. Returns True iff the
    reservation succeeded (i.e. there was budget left).
    """
    day = day or _date.today()
    await ensure_budget_day(day, vertical=vertical)
    async with async_session_factory() as session:
        result = await session.execute(
            text("SELECT reserve_budget(:day, :vertical, CAST(:amt AS NUMERIC))"),
            {"day": day, "vertical": vertical, "amt": est_usd},
        )
        ok = bool(result.scalar_one())
        await session.commit()
        return ok


async def settle(run_id: int, actual_usd: float, *, vertical: str = "gossip") -> None:
    """Reconcile actual spend: add to ``actual_usd`` and log a ledger row."""
    day = _date.today()
    async with async_session_factory() as session:
        await session.execute(
            text(
                "UPDATE budget_day SET actual_usd = actual_usd + CAST(:amt AS NUMERIC) "
                "WHERE day = :day AND vertical = :vertical"
            ),
            {"amt": actual_usd, "day": day, "vertical": vertical},
        )
        await session.execute(
            text(
                """
                INSERT INTO spend_ledger (run_id, cost_usd, kind)
                VALUES (:run_id, :amt, 'actual')
                """
            ),
            {"run_id": run_id, "amt": actual_usd},
        )
        await session.commit()


# --- escalation ---------------------------------------------------------------

async def can_escalate(
    day: _date | None = None, *, vertical: str = "gossip"
) -> bool:
    """True iff today's escalation count for ``vertical`` is still below the cap."""
    day = day or _date.today()
    await ensure_budget_day(day, vertical=vertical)
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT escalations < escalation_cap FROM budget_day "
                "WHERE day = :day AND vertical = :vertical"
            ),
            {"day": day, "vertical": vertical},
        )
        return bool(result.scalar_one_or_none())


async def record_escalation(
    day: _date | None = None, *, vertical: str = "gossip"
) -> None:
    """Increment the escalation counter for ``vertical``."""
    day = day or _date.today()
    await ensure_budget_day(day, vertical=vertical)
    async with async_session_factory() as session:
        await session.execute(
            text(
                "UPDATE budget_day SET escalations = escalations + 1 "
                "WHERE day = :day AND vertical = :vertical"
            ),
            {"day": day, "vertical": vertical},
        )
        await session.commit()


# --- kill-switch --------------------------------------------------------------

async def kill_switch_active(vertical: str = "") -> bool:
    """True iff the kill-switch for ``vertical`` (or global fallback) is ``'on'``.

    Checks ``killswitch:{vertical}`` first; if no per-vertical key exists,
    falls back to the global ``kill_switch`` key for backward compatibility.
    When ``vertical`` is empty, checks only the global key.
    """
    async with async_session_factory() as session:
        if vertical:
            result = await session.execute(
                text("SELECT value FROM system_state WHERE key = :k"),
                {"k": f"killswitch:{vertical}"},
            )
            value = result.scalar_one_or_none()
            if value is not None:
                return value.strip().lower() == "on"
            # Fall through to global kill_switch
        result = await session.execute(
            text("SELECT value FROM system_state WHERE key = 'kill_switch'")
        )
        value = result.scalar_one_or_none()
    return (value or "").strip().lower() == "on"


async def trip_kill_switch(reason: str = "", *, vertical: str = "") -> None:
    """Set a kill-switch to ``'on'`` with an optional reason (upsert).

    If ``vertical`` is given, sets ``killswitch:{vertical}``. Otherwise sets
    the global ``kill_switch`` key.
    """
    key = f"killswitch:{vertical}" if vertical else "kill_switch"
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO system_state (key, value, reason, updated_at)
                VALUES (:key, 'on', :reason, now())
                ON CONFLICT (key)
                DO UPDATE SET value = 'on', reason = :reason, updated_at = now()
                """
            ),
            {"key": key, "reason": reason or None},
        )
        await session.commit()


async def reset_kill_switch(*, vertical: str = "") -> None:
    """Set a kill-switch back to ``'off'`` (upsert).

    If ``vertical`` is given, resets ``killswitch:{vertical}``. Otherwise resets
    the global ``kill_switch`` key.
    """
    key = f"killswitch:{vertical}" if vertical else "kill_switch"
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO system_state (key, value, reason, updated_at)
                VALUES (:key, 'off', NULL, now())
                ON CONFLICT (key)
                DO UPDATE SET value = 'off', reason = NULL, updated_at = now()
                """
            ),
            {"key": key},
        )
        await session.commit()


# --- status -------------------------------------------------------------------

async def budget_status(
    day: _date | None = None, *, vertical: str = "gossip"
) -> dict:
    """Return a snapshot of the budget + kill-switch state for ``vertical``."""
    day = day or _date.today()
    await ensure_budget_day(day, vertical=vertical)
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT day, vertical, reserved_usd, actual_usd, ceiling_usd,
                           escalations, escalation_cap
                    FROM budget_day WHERE day = :day AND vertical = :vertical
                    """
                ),
                {"day": day, "vertical": vertical},
            )
        ).mappings().one()
        ks = await kill_switch_active(vertical=vertical)

    reserved = float(row["reserved_usd"])
    actual = float(row["actual_usd"])
    ceiling = float(row["ceiling_usd"])
    return {
        "day": row["day"].isoformat(),
        "vertical": row["vertical"],
        "reserved_usd": reserved,
        "actual_usd": actual,
        "ceiling_usd": ceiling,
        "escalations": int(row["escalations"]),
        "escalation_cap": int(row["escalation_cap"]),
        "remaining": ceiling - reserved,
        "kill_switch": ks,
    }
