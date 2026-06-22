"""Database engine, session factory, and the ORM declarative base.

A single ``postgresql+psycopg`` URL drives both the async application engine
(here) and the synchronous Alembic engine (in ``alembic/env.py``). psycopg 3
supports both modes natively, so no separate async driver is needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Async engine.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession`, committing on success and rolling back
    on error. Usable as a FastAPI-style dependency or via ``async for``.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> bool:
    """Open a connection and run ``SELECT 1``. Returns True on success.

    Used by the CLI / verification to confirm the database is reachable.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar_one() == 1


# --- Synchronous engine -------------------------------------------------------
# The ToolBus exposes a *synchronous* ``call()`` (plan §3.5) and is driven by
# the equally-synchronous OpenAI SDK in the research loop. Rather than nest event
# loops, those callers use a plain sync engine. psycopg 3 serves both sync and
# async from the same ``postgresql+psycopg`` URL, so no second driver is needed.
# Construction is lazy + cached so importing this module never opens a connection.


@lru_cache
def get_sync_engine():
    """Return the process-wide synchronous SQLAlchemy engine (lazy, cached)."""
    return create_engine(settings.database_url, echo=False, pool_pre_ping=True)


@lru_cache
def get_sync_session_factory() -> sessionmaker[Session]:
    """Return a cached sync ``sessionmaker`` bound to :func:`get_sync_engine`."""
    return sessionmaker(bind=get_sync_engine(), expire_on_commit=False, autoflush=False)


def get_sync_db() -> Iterator[Session]:
    """Yield a sync :class:`Session`, committing on success, rolling back on error."""
    with get_sync_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
