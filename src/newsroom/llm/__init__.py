"""LLM subsystem public API: client, Hermes tool-calling, and the tool bus.

Phase 0/1 use :class:`PyToolBus`; Phase 2 swaps in an MCP-backed ``ToolBus`` with
no pipeline changes (the seam is :class:`ToolBus`).
"""

from __future__ import annotations

from .client import (
    ChatResult,
    LLMClient,
    LLMError,
    get_client,
    provider_breaker_snapshot,
)
from .hermes import (
    ToolCall,
    build_system_prompt,
    chat_parse_tools,
    format_tool_response,
    format_tool_responses,
    format_tools_block,
    parse_fail_rate,
    parse_response,
    parse_stats,
    parse_tool_calls,
    tool_spec,
)
from .mcp_stub import McpToolBus
from .toolbus import PyToolBus, ToolBus, tool_specs

__all__ = [
    # client
    "ChatResult",
    "LLMClient",
    "LLMError",
    "get_client",
    "provider_breaker_snapshot",
    # hermes
    "ToolCall",
    "build_system_prompt",
    "chat_parse_tools",
    "format_tool_response",
    "format_tool_responses",
    "format_tools_block",
    "parse_fail_rate",
    "parse_response",
    "parse_stats",
    "parse_tool_calls",
    "tool_spec",
    # toolbus
    "McpToolBus",
    "PyToolBus",
    "ToolBus",
    "tool_specs",
]
