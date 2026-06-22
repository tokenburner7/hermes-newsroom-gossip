"""ToolBus interface + the Phase-0/1 in-process implementation (plan §3.5, Hermes C1).

``ToolBus`` is the MCP-decoupling abstraction: the pipeline only ever depends on
``ToolBus.call(name, arguments) -> dict``. Phase 0/1 wire it to :class:`PyToolBus`
(direct Python over Postgres); Phase 2 swaps in an ``McpToolBus`` that satisfies the
same Protocol, with zero pipeline changes.

Four tools, matching the signatures the Phase-2 MCP server will expose:

* ``search_sources(query, time_window_minutes=60, limit=20)``
* ``fetch_url(url, max_chars=8000)``
* ``query_memory(query, k=8)`` — Phase-0 placeholder: a plain text search
* ``record_claims(claims: list[dict])`` — locks provenance before drafting

All tools are synchronous (the Protocol is sync) and run over the sync engine in
:mod:`newsroom.db`. ``call()`` never raises: tool errors come back as
``{"error": ...}`` so the model can recover within the tool loop.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import func, or_, select

from ..db import get_sync_session_factory
from ..models import Claim, Source
from .hermes import tool_spec

log = logging.getLogger(__name__)

SOURCE_CLASS = "arxiv"  # Phase 0 corpus


@runtime_checkable
class ToolBus(Protocol):
    """The single seam the pipeline depends on (PyToolBus today, McpToolBus in Phase 2)."""

    def call(self, name: str, arguments: dict) -> dict: ...


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_specs() -> list[dict]:
    """OpenAI-style declarations for the four tools (for the Hermes ``<tools>`` block)."""
    return [
        tool_spec(
            "search_sources",
            "Search ingested sources (title + text) for a query, restricted to a "
            "recent time window. Returns matching sources with snippets.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms."},
                    "time_window_minutes": {
                        "type": "integer",
                        "description": "Only sources seen within this many minutes.",
                        "default": 60,
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        ),
        tool_spec(
            "fetch_url",
            "Fetch the cleaned full text of a previously-ingested source by its URL.",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 8000},
                },
                "required": ["url"],
            },
        ),
        tool_spec(
            "query_memory",
            "Query prior knowledge/memory for relevant material (Phase-0: text search "
            "over the source corpus).",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        ),
        tool_spec(
            "record_claims",
            "Persist extracted claims with their supporting source spans BEFORE "
            "drafting, locking provenance. Each claim needs claim_text, source_url, "
            "and supporting_span (verbatim from the source).",
            {
                "type": "object",
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim_text": {"type": "string"},
                                "source_id": {"type": "integer"},
                                "source_url": {"type": "string"},
                                "supporting_span": {"type": "string"},
                                "confidence": {"type": "number"},
                            },
                            "required": ["claim_text", "source_url", "supporting_span"],
                        },
                    }
                },
                "required": ["claims"],
            },
        ),
    ]


class PyToolBus:
    """Direct-Python ToolBus over Postgres (Phase 0/1)."""

    #: Tool names this bus implements.
    TOOLS = ("search_sources", "fetch_url", "query_memory", "record_claims")

    def __init__(
        self,
        *,
        run_id: int | None = None,
        session_factory=None,
        source_classes: list[str] | None = None,
    ) -> None:
        self._run_id = run_id
        self._session_factory = session_factory or get_sync_session_factory()
        # When set, _search_sources scopes to these source classes (per-vertical).
        # When None, falls back to the module-level SOURCE_CLASS (backward compat).
        self._source_classes = source_classes

    # -- dispatch -------------------------------------------------------------

    def call(self, name: str, arguments: dict) -> dict:
        """Dispatch a tool call. Never raises; returns ``{"error": ...}`` on failure."""
        handler = {
            "search_sources": self._search_sources,
            "fetch_url": self._fetch_url,
            "query_memory": self._query_memory,
            "record_claims": self._record_claims,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}", "available": list(self.TOOLS)}
        try:
            return handler(arguments or {})
        except Exception as exc:  # noqa: BLE001 — feed errors back to the model
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tools ----------------------------------------------------------------

    def _search_sources(self, args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        window = int(args.get("time_window_minutes", 60))
        limit = max(1, min(int(args.get("limit", 20)), 100))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)

        terms = [t for t in query.split() if t]
        with self._session_factory() as session:
            if self._source_classes:
                stmt = select(Source).where(
                    Source.source_class.in_(self._source_classes)
                )
            else:
                stmt = select(Source).where(Source.source_class == SOURCE_CLASS)
            # Recency: published_at when known, else retrieved_at.
            recency = func.coalesce(Source.published_at, Source.retrieved_at)
            stmt = stmt.where(recency >= cutoff)
            if terms:
                conds = []
                for term in terms:
                    like = f"%{term}%"
                    conds.append(Source.title.ilike(like))
                    conds.append(Source.cleaned_text.ilike(like))
                stmt = stmt.where(or_(*conds))
            stmt = stmt.order_by(recency.desc()).limit(limit * 3)
            rows = session.execute(stmt).scalars().all()

        scored = []
        for src in rows:
            hay = f"{src.title or ''} {src.cleaned_text or ''}".lower()
            hits = sum(hay.count(t.lower()) for t in terms) if terms else 1
            scored.append((hits, src))
        scored.sort(key=lambda x: x[0], reverse=True)

        results = [self._source_brief(src) for hits, src in scored[:limit] if hits > 0 or not terms]
        return {"query": query, "count": len(results), "results": results}

    def _query_memory(self, args: dict) -> dict:
        # Phase-0 placeholder: same text search, no time window, top-k.
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        k = max(1, min(int(args.get("k", 8)), 50))
        out = self._search_sources({"query": query, "time_window_minutes": 10**9, "limit": k})
        return {"query": query, "k": k, "results": out.get("results", []), "note": "phase0-placeholder"}

    def _fetch_url(self, args: dict) -> dict:
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "url is required"}
        max_chars = max(1, int(args.get("max_chars", 8000)))
        with self._session_factory() as session:
            src = session.execute(select(Source).where(Source.url == url)).scalar_one_or_none()
        if src is None:
            return {"url": url, "found": False, "error": "no ingested source with that url"}
        text = src.cleaned_text or ""
        return {
            "url": url,
            "found": True,
            "source_id": src.id,
            "external_id": src.external_id,
            "title": src.title,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def _record_claims(self, args: dict) -> dict:
        claims = args.get("claims")
        if not isinstance(claims, list) or not claims:
            return {"error": "claims must be a non-empty list"}

        prepared: list[Claim] = []
        for i, c in enumerate(claims):
            if not isinstance(c, dict):
                return {"error": f"claim[{i}] is not an object"}
            claim_text = (c.get("claim_text") or "").strip()
            source_url = (c.get("source_url") or "").strip()
            span = (c.get("supporting_span") or "").strip()
            missing = [k for k, v in (("claim_text", claim_text), ("source_url", source_url), ("supporting_span", span)) if not v]
            if missing:
                return {"error": f"claim[{i}] missing required fields: {missing}"}
            prepared.append(
                Claim(
                    run_id=c.get("run_id", self._run_id),
                    claim_text=claim_text,
                    source_id=c.get("source_id"),
                    source_url=source_url,
                    supporting_span=span,
                    span_sha256=_sha256(span),
                    confidence=c.get("confidence"),
                )
            )

        with self._session_factory() as session:
            session.add_all(prepared)
            session.commit()
            ids = [c.id for c in prepared]
        log.info("recorded %d claims (run_id=%s)", len(ids), self._run_id)
        return {"inserted": len(ids), "claim_ids": ids}

    @staticmethod
    def _source_brief(src: Source) -> dict:
        text = src.cleaned_text or ""
        return {
            "source_id": src.id,
            "external_id": src.external_id,
            "url": src.url,
            "title": src.title,
            "published_at": src.published_at.isoformat() if src.published_at else None,
            "snippet": text[:280],
        }
