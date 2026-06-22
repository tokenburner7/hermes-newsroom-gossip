"""Hermes-4 tool-calling: parse ``<tool_call>`` blocks, emit ``<tools>`` + ``<tool_response>``.

Hermes models do *function calling in text*: the system prompt declares tools in a
``<tools>…</tools>`` JSON block, the model replies with one or more
``<tool_call>{json}</tool_call>`` blocks, and tool results are fed back wrapped in
``<tool_response>…</tool_response>``.

The plan (§9) flags this parser as "the single most likely source of silent
breakage" — multi-call turns, prose around the tags, and malformed JSON all occur
in the wild. So the parser is defensive (regex-extract every block, ``json.loads``
each independently, never raise) and the **parse-fail rate is tracked as an SLO**
from Day 3 via :func:`parse_fail_rate`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle; only needed for type hints
    from .client import ChatResult, LLMClient

log = logging.getLogger(__name__)

# Every <tool_call> … </tool_call> block, non-greedy, across newlines.
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

# The canonical Hermes function-calling instruction (paired with the tools block).
_HERMES_INSTRUCTIONS = (
    "You are a function calling AI model. You are provided with function "
    "signatures within <tools></tools> XML tags. You may call one or more "
    "functions to assist with the user query. Don't make assumptions about what "
    "values to plug into functions. For each function call, return a JSON object "
    "with the function name and arguments within <tool_call></tool_call> XML "
    "tags as follows:\n"
    "<tool_call>\n"
    '{"name": <function-name>, "arguments": <args-dict>}\n'
    "</tool_call>"
)


@dataclass(slots=True)
class ToolCall:
    """A single parsed tool invocation requested by the model."""

    name: str
    arguments: dict
    raw: str  # the raw JSON text inside the <tool_call> block


@dataclass(slots=True)
class ParseResult:
    """Outcome of parsing the ``<tool_call>`` blocks out of a model response."""

    calls: list[ToolCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocks_found: int = 0

    @property
    def had_blocks(self) -> bool:
        return self.blocks_found > 0

    @property
    def malformed(self) -> bool:
        """True iff at least one block was present but failed to parse."""
        return self.blocks_found > 0 and len(self.calls) < self.blocks_found


# --- Parse-fail SLO accounting -----------------------------------------------
# Process-wide counters; coarse but enough to alert on the breakage the plan warns
# about. ``responses_with_blocks`` is the denominator (turns that *attempted* tool
# calls), ``responses_with_malformed`` the numerator.
_STATS = {"responses_with_blocks": 0, "responses_with_malformed": 0, "blocks": 0, "block_failures": 0}


def parse_fail_rate() -> float:
    """Fraction of individual ``<tool_call>`` blocks that failed to parse."""
    blocks = _STATS["blocks"]
    return (_STATS["block_failures"] / blocks) if blocks else 0.0


def parse_stats() -> dict[str, int | float]:
    """Snapshot of the parse-fail SLO counters (for the CLI / monitoring)."""
    return {**_STATS, "block_fail_rate": parse_fail_rate()}


def reset_parse_stats() -> None:
    """Zero the counters (useful in tests)."""
    for k in _STATS:
        _STATS[k] = 0


def parse_response(text: str) -> ParseResult:
    """Extract and JSON-parse every ``<tool_call>`` block in ``text``.

    Never raises: malformed blocks are recorded in ``errors`` and counted toward
    the parse-fail SLO. Tolerates prose around the tags and multi-call turns.
    """
    result = ParseResult()
    blocks = TOOL_CALL_RE.findall(text or "")
    result.blocks_found = len(blocks)
    _STATS["blocks"] += len(blocks)

    for raw in blocks:
        snippet = raw.strip()
        try:
            obj = json.loads(snippet)
        except json.JSONDecodeError as exc:
            _STATS["block_failures"] += 1
            result.errors.append(f"json: {exc}")
            log.warning("malformed <tool_call> JSON: %s | block=%r", exc, snippet[:200])
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            _STATS["block_failures"] += 1
            result.errors.append("schema: missing 'name' or not an object")
            log.warning("invalid <tool_call> shape: %r", snippet[:200])
            continue
        args = obj.get("arguments", {})
        if not isinstance(args, dict):
            _STATS["block_failures"] += 1
            result.errors.append("schema: 'arguments' is not an object")
            log.warning("non-dict arguments in <tool_call>: %r", snippet[:200])
            continue
        result.calls.append(ToolCall(name=str(obj["name"]), arguments=args, raw=snippet))

    if result.had_blocks:
        _STATS["responses_with_blocks"] += 1
        if result.malformed:
            _STATS["responses_with_malformed"] += 1
    return result


def parse_tool_calls(text: str) -> list[ToolCall]:
    """Convenience wrapper returning just the successfully-parsed tool calls."""
    return parse_response(text).calls


# --- Formatting (declaration + responses) ------------------------------------


def format_tools_block(tools: list[dict]) -> str:
    """Render tool specs into the Hermes ``<tools>…</tools>`` JSON block.

    Accepts OpenAI-style ``{"type": "function", "function": {...}}`` dicts (or a
    bare function dict). One compact JSON object per line, as Hermes expects.
    """
    lines = ["<tools>"]
    for tool in tools:
        spec = tool.get("function", tool) if isinstance(tool, dict) else tool
        lines.append(json.dumps(spec, ensure_ascii=False))
    lines.append("</tools>")
    return "\n".join(lines)


def build_system_prompt(tools: list[dict], *, preamble: str = "") -> str:
    """Build a full Hermes function-calling system message for ``tools``.

    ``preamble`` (e.g. the Research-Synthesis role instructions) is prepended
    before the standard function-calling instructions and the ``<tools>`` block.
    """
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.strip())
    parts.append(_HERMES_INSTRUCTIONS)
    parts.append(format_tools_block(tools))
    return "\n\n".join(parts)


def tool_spec(name: str, description: str, parameters: dict) -> dict:
    """Build one OpenAI-style function tool spec (JSON-Schema ``parameters``)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def format_tool_response(content: object, *, name: str | None = None) -> str:
    """Wrap a tool result in a ``<tool_response>`` block (JSON-serialized).

    ``name`` is included when given so multi-tool turns stay attributable.
    """
    payload: object
    if name is not None and isinstance(content, dict):
        payload = {"name": name, "content": content}
    elif name is not None:
        payload = {"name": name, "content": content}
    else:
        payload = content
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"<tool_response>\n{body}\n</tool_response>"


def format_tool_responses(items: list[tuple[str, object]]) -> str:
    """Format several (tool_name, result) pairs as stacked ``<tool_response>`` blocks."""
    return "\n".join(format_tool_response(content, name=name) for name, content in items)


# --- Parse-or-retry (plan §4 Day 3: "parse-or-retry max 2") ------------------

_RETRY_CORRECTION = (
    "Your previous reply contained a malformed <tool_call> block. Reply again "
    "with ONLY valid <tool_call>{...}</tool_call> blocks — each a JSON object "
    'with "name" and "arguments" keys, and nothing else.'
)


def chat_parse_tools(
    client: "LLMClient",
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    max_retries: int = 2,
    **chat_kwargs,
) -> "ChatResult":
    """Call the model and parse tool calls, retrying on malformed JSON (≤ ``max_retries``).

    A *retry* happens only when the response contained ``<tool_call>`` blocks that
    failed to parse — a plain prose answer is returned as-is. On the final attempt
    the (possibly partial) result is returned regardless, with the failure logged.
    The returned :class:`ChatResult` has ``tool_calls`` populated.
    """
    convo = list(messages)
    last: "ChatResult | None" = None
    for attempt in range(max_retries + 1):
        result = client.chat(convo, model=model, tools=tools, parse_tools=False, **chat_kwargs)
        last = result
        parsed = parse_response(result.text)
        result.tool_calls = parsed.calls
        if not parsed.malformed:
            return result
        if attempt < max_retries:
            log.warning(
                "tool-call parse retry %d/%d (errors=%s)", attempt + 1, max_retries, parsed.errors
            )
            convo = [*convo, {"role": "assistant", "content": result.text}, {"role": "user", "content": _RETRY_CORRECTION}]
    log.error("tool-call parsing failed after %d retries; returning partial result", max_retries)
    assert last is not None
    return last
