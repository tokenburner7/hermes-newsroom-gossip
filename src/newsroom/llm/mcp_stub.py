"""MCP tool-bus stub — the Phase-2 swap target for :class:`PyToolBus` (plan §3.5).

The pipeline depends only on the :class:`~newsroom.llm.toolbus.ToolBus` Protocol
(``call(name, arguments) -> dict``). Phase 0/1 wire that to :class:`PyToolBus`
(direct Python over Postgres). Phase 2 moves the four tools behind an MCP server
so they can be hosted/scaled independently and shared with other agents.

Temporal is the real blocker for standing up that MCP server, so this is a
**stub**: :class:`McpToolBus` satisfies the exact same Protocol but, for now,
delegates each ``call`` to a wrapped :class:`PyToolBus` in-process (logging the
intended transport once). When the MCP server is ready, only the body of
:meth:`call` changes — open a session over the configured transport and proxy
the call — with **zero** pipeline changes, because the seam is unchanged::

    bus = McpToolBus(run_id=run_id)          # was: PyToolBus(run_id=run_id)
    result = client.chat_with_tools(messages, model=..., tools=tool_specs(), bus=bus)

The ``mcp`` SDK is already a project dependency; the real implementation will use
``mcp.client.stdio`` (or an HTTP transport) and ``mcp.ClientSession``.
"""

from __future__ import annotations

import logging

from .toolbus import PyToolBus, tool_specs

log = logging.getLogger(__name__)

#: Transport the Phase-2 MCP server will speak. Only stdio is planned for the
#: first cut (the server runs as a child process of the worker).
DEFAULT_TRANSPORT = "stdio"


class McpToolBus:
    """ToolBus over MCP (stub). Same interface as :class:`PyToolBus`.

    Phase-2 stub behaviour: wraps a :class:`PyToolBus` and forwards every call to
    it in-process. The constructor mirrors ``PyToolBus`` so call sites can swap one
    for the other without changing their arguments.

    Parameters
    ----------
    run_id, session_factory:
        Forwarded to the wrapped :class:`PyToolBus` when ``delegate`` is not given.
    delegate:
        An existing tool bus to forward to (anything with ``call``). Defaults to a
        freshly constructed :class:`PyToolBus`.
    transport:
        The MCP transport label, logged for visibility (default :data:`DEFAULT_TRANSPORT`).
    """

    #: The four tools this bus exposes — the MCP server will declare the same set.
    TOOLS = PyToolBus.TOOLS

    def __init__(
        self,
        *,
        run_id: int | None = None,
        session_factory=None,
        delegate=None,
        transport: str = DEFAULT_TRANSPORT,
    ) -> None:
        self._delegate = delegate or PyToolBus(
            run_id=run_id, session_factory=session_factory
        )
        self._transport = transport
        log.info("MCP transport: %s (stub — Phase 2)", transport)

    def call(self, name: str, arguments: dict) -> dict:
        """Dispatch a tool call. Same contract as :class:`PyToolBus.call`.

        Stub: forwards to the wrapped bus in-process. Phase 2 replaces this body
        with an MCP ``session.call_tool(name, arguments)`` round-trip. Never raises
        — tool errors come back as ``{"error": ...}`` so the model can recover.
        """
        return self._delegate.call(name, arguments)

    def tool_specs(self) -> list[dict]:
        """OpenAI-style declarations for the tools (delegates to :func:`tool_specs`)."""
        return tool_specs()


__all__ = ["McpToolBus", "DEFAULT_TRANSPORT"]
