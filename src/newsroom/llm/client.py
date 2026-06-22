"""LLM client: OpenAI-compatible wrapper with DeepSeek→OpenRouter failover.

A thin wrapper over the ``openai`` SDK pointed at DeepSeek's OpenAI-compatible
endpoint (primary) with OpenRouter as failover (plan §3.5). On a retryable
provider error — HTTP 429 or 5xx, or a connection/timeout — the call falls over
to the next configured provider. Token usage is captured on every call into
:class:`ChatResult` (the substrate for the Phase-1 cost ledger).

Two calling styles:

* :meth:`LLMClient.chat` — a single completion. ``tools`` is *not* forwarded as
  native function-calling here (it was the seam for the legacy Hermes XML format);
  use ``parse_tools=True`` only if you are parsing ``<tool_call>`` text yourself.
* :meth:`LLMClient.chat_with_tools` — DeepSeek **native** OpenAI function calling.
  ``tools`` is forwarded to the provider, returned tool calls are executed against
  a :class:`~newsroom.llm.toolbus.ToolBus`, results are fed back as ``tool``-role
  messages, and the loop runs until the model stops calling tools or ``max_turns``
  is hit. This is the path the DeepSeek pipeline uses — no Hermes XML.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import openai
from openai import OpenAI

from ..circuit_breaker import CircuitBreakerRegistry
from ..config import settings
from .hermes import ToolCall, parse_tool_calls
from .. import telemetry

if TYPE_CHECKING:  # avoid importing the DB-backed tool bus at client import time
    from .toolbus import ToolBus

log = logging.getLogger(__name__)


def _last_user_text(messages: list[dict]) -> str:
    """Best-effort: the most recent user message content (for LLM-trace input)."""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


class LLMError(RuntimeError):
    """Raised when every configured provider fails for a single chat call."""


@dataclass(slots=True)
class ChatResult:
    """The result of one chat completion (plan §3.5).

    For :meth:`LLMClient.chat_with_tools`, ``in_tokens``/``out_tokens`` are summed
    across every turn of the tool loop, ``tool_turns`` counts how many model turns
    requested tools, and ``tool_results`` logs each executed call as
    ``{"name", "arguments", "result"}`` (in call order).
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    in_tokens: int = 0
    out_tokens: int = 0
    model: str = ""
    provider: str = ""
    finish_reason: str | None = None
    tool_turns: int = 0
    tool_results: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class _Provider:
    """A configured upstream: a name, an OpenAI client, and a model-id mapper."""

    name: str
    client: OpenAI
    # (canonical_model) -> provider model id, or None if this provider can't
    # serve the model (failover then skips it rather than make a doomed call).
    model_for: "callable"


# Canonical model → OpenRouter slug. OpenRouter carries most models including
# DeepSeek, so it serves as failover.  When model_primary is already available
# on the primary provider (DeepSeek), OpenRouter acts as secondary.
_OPENROUTER_MODEL_MAP: dict[str, str] = {}
_OPENROUTER_UNAVAILABLE: frozenset[str] = frozenset()


def _openrouter_model(model: str) -> str | None:
    """Map a canonical model id to its OpenRouter slug, or None if unavailable."""
    if model in _OPENROUTER_UNAVAILABLE:
        return None
    return _OPENROUTER_MODEL_MAP.get(model, model)


def _is_retryable(exc: Exception) -> bool:
    """True for provider-level failures where *another* provider may still succeed.

    Beyond the plan's 429/5xx, this also covers auth (401), permission (403) and
    model-not-found (404): if one provider rejects our key or lacks the model,
    falling over to the next configured provider is the resilient move. If every
    provider fails the caller still gets an :class:`LLMError`.
    """
    if isinstance(
        exc,
        (
            openai.RateLimitError,  # 429
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,  # 500
            openai.AuthenticationError,  # 401 — dead/placeholder key
            openai.PermissionDeniedError,  # 403
            openai.NotFoundError,  # 404 — model not on this provider
        ),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


def _is_breaker_trip(exc: Exception) -> bool:
    """True for provider-health failures that should count toward the circuit breaker.

    Narrower than :func:`_is_retryable`: only 429 / 5xx / timeout / connection — the
    failures the plan's per-provider breaker is meant to stop hammering (§6). Auth
    (401), permission (403) and not-found (404) are *retryable for failover* but are
    configuration faults a 30s half-open probe won't fix, so they must not trip it.
    """
    if isinstance(
        exc,
        (
            openai.RateLimitError,  # 429
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,  # 500
        ),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


# One breaker per provider name (DeepSeek, OpenRouter), shared across LLMClient
# instances so the state survives client re-creation within a process (plan §6).
_provider_breakers = CircuitBreakerRegistry()


def provider_breaker_snapshot() -> dict[str, dict]:
    """Per-provider circuit-breaker state (name → snapshot)."""
    return _provider_breakers.snapshot()


class LLMClient:
    """Multi-provider chat client. Tries DeepSeek, then OpenRouter on failover."""

    def __init__(self) -> None:
        self._providers = self._build_providers()
        if not self._providers:
            log.warning(
                "no LLM providers configured; set DEEPSEEK_API_KEY / OPENROUTER_API_KEY"
            )

    def _build_providers(self) -> list[_Provider]:
        providers: list[_Provider] = []
        if settings.deepseek_configured:
            providers.append(
                _Provider(
                    name="deepseek",
                    client=OpenAI(
                        api_key=settings.deepseek_api_key,
                        base_url=settings.deepseek_base_url,
                        timeout=settings.llm_timeout_s,
                        max_retries=settings.llm_max_retries,
                    ),
                    model_for=lambda m: m,  # canonical slug works as-is on DeepSeek
                )
            )
        if settings.openrouter_configured:
            providers.append(
                _Provider(
                    name="openrouter",
                    client=OpenAI(
                        api_key=settings.openrouter_api_key,
                        base_url=settings.openrouter_base_url,
                        timeout=settings.llm_timeout_s,
                        max_retries=settings.llm_max_retries,
                    ),
                    model_for=_openrouter_model,
                )
            )
        return providers

    @property
    def providers(self) -> list[str]:
        """Names of the configured providers, in failover order."""
        return [p.name for p in self._providers]

    # -- provider failover --------------------------------------------------

    def _call_providers(self, *, model: str, kwargs: dict, provider: str | None = None):
        """Run ``chat.completions.create`` across providers with failover.

        Returns ``(response, provider_name, provider_model)`` from the first
        provider that succeeds. Raises :class:`LLMError` if none do. Shared by
        both :meth:`chat` and :meth:`chat_with_tools`.

        ``provider`` pins the call to a single named provider (e.g. ``"openrouter"``
        for the cross-family eval judge): other providers are skipped entirely, so
        a failure raises rather than silently falling back to the primary family.
        """
        if not self._providers:
            raise LLMError("no LLM providers configured")

        candidates = self._providers
        if provider is not None:
            candidates = [p for p in self._providers if p.name == provider]
            if not candidates:
                raise LLMError(f"provider {provider!r} not configured")

        errors: list[str] = []
        for provider_obj in candidates:
            breaker = _provider_breakers.get(provider_obj.name)
            if not breaker.allow():
                msg = (
                    f"{provider_obj.name}: circuit breaker {breaker.state.value} "
                    f"(retry in ~{breaker.retry_after_s():.0f}s)"
                )
                log.warning("skipping provider with open breaker: %s", msg)
                errors.append(msg)
                continue

            provider_model = provider_obj.model_for(model)
            if provider_model is None:
                log.info(
                    "provider %s has no equivalent for model %s; skipping",
                    provider_obj.name, model,
                )
                errors.append(f"{provider_obj.name}: no equivalent for {model}")
                continue
            try:
                resp = provider_obj.client.chat.completions.create(
                    model=provider_model, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 — classify then decide
                msg = f"{provider_obj.name}({provider_model}): {type(exc).__name__}: {exc}"
                if _is_retryable(exc):
                    # Only provider-health failures (429/5xx/timeout/connection) count
                    # toward the breaker; auth/404 fail over without tripping it.
                    if _is_breaker_trip(exc):
                        breaker.record_failure()
                        log.warning(
                            "retryable LLM error (breaker %s, %d consecutive), failing over: %s",
                            breaker.state.value, breaker.consecutive_failures, msg,
                        )
                    else:
                        log.warning("retryable LLM error, failing over: %s", msg)
                    errors.append(msg)
                    continue
                log.error("non-retryable LLM error: %s", msg)
                raise LLMError(msg) from exc
            breaker.record_success()
            return resp, provider_obj.name, provider_model

        raise LLMError("all providers failed: " + " | ".join(errors))

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.4,
        parse_tools: bool = False,
        provider: str | None = None,
    ) -> ChatResult:
        """Run a single chat completion, failing over across providers on 429/5xx.

        This method does **not** forward ``tools`` as native function-calling — it
        is the plain-completion / JSON-mode path (``response_format`` *is*
        forwarded). For native DeepSeek tool calling use :meth:`chat_with_tools`.
        Set ``parse_tools=True`` to populate :attr:`ChatResult.tool_calls` by
        parsing legacy ``<tool_call>`` text out of the response.

        Raises :class:`LLMError` if no provider succeeds.
        """
        kwargs: dict = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        resp, provider_name, provider_model = self._call_providers(
            model=model, kwargs=kwargs, provider=provider
        )
        result = self._to_result(resp, provider=provider_name, model=provider_model)
        if parse_tools:
            result.tool_calls = parse_tool_calls(result.text)
        telemetry.record_llm_call(
            model=result.model, provider=result.provider,
            in_tokens=result.in_tokens, out_tokens=result.out_tokens,
            input_text=_last_user_text(messages), output_text=result.text,
        )
        log.info(
            "llm ok provider=%s model=%s in=%d out=%d",
            result.provider, result.model, result.in_tokens, result.out_tokens,
        )
        return result

    def chat_with_tools(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict],
        bus: "ToolBus",
        max_turns: int = 12,
        max_tokens: int = 2048,
        temperature: float = 0.4,
        detect_cycles: bool = True,
    ) -> ChatResult:
        """Drive a DeepSeek **native** function-calling loop against ``bus``.

        ``tools`` are OpenAI-style specs (``{"type":"function","function":{...}}``,
        exactly what :func:`newsroom.llm.toolbus.tool_specs` returns) and are
        forwarded to the provider as the native ``tools`` parameter — no Hermes
        XML. Each turn:

        1. call the model with ``tool_choice="auto"``;
        2. if it returns no tool calls, stop and return the text;
        3. otherwise execute every requested call via ``bus.call(name, args)``,
           append the assistant message and one ``tool``-role message per call,
           and loop.

        Guards: a hard cap of ``max_turns`` tool-executing turns (plan §4: 12),
        and — when ``detect_cycles`` — repeated *identical* ``(name, arguments)``
        calls short-circuit to an error result that tells the model to stop
        repeating. If ``max_turns`` is exhausted while the model still wants
        tools, one final call with tools disabled extracts a closing answer.

        Token usage is summed across turns; :attr:`ChatResult.tool_turns` and
        :attr:`ChatResult.tool_results` record the loop.
        """
        convo: list[dict] = [dict(m) for m in messages]
        total_in = total_out = 0
        tool_turns = 0
        invocations: list[dict] = []
        seen: set[tuple[str, str]] = set()
        provider_name = resolved_model = ""
        finish_reason: str | None = None

        def base_kwargs(*, with_tools: bool) -> dict:
            k: dict = {
                "messages": convo,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if with_tools:
                k["tools"] = tools
                k["tool_choice"] = "auto"
            return k

        for _turn in range(max_turns):
            resp, provider_name, resolved_model = self._call_providers(
                model=model, kwargs=base_kwargs(with_tools=True)
            )
            choice = resp.choices[0]
            msg = choice.message
            finish_reason = getattr(choice, "finish_reason", None)
            total_in, total_out = self._add_usage(resp, total_in, total_out)
            convo.append(self._assistant_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                telemetry.record_llm_call(
                    name="llm.tool_loop", model=resolved_model, provider=provider_name,
                    in_tokens=total_in, out_tokens=total_out,
                    input_text=_last_user_text(messages), output_text=msg.content or "",
                )
                return ChatResult(
                    text=msg.content or "",
                    in_tokens=total_in,
                    out_tokens=total_out,
                    model=resolved_model,
                    provider=provider_name,
                    finish_reason=finish_reason,
                    tool_turns=tool_turns,
                    tool_results=invocations,
                )

            tool_turns += 1
            for tc in tool_calls:
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    log.warning("non-JSON tool arguments for %s: %r", name, raw_args[:200])
                    args = {}

                sig = (name, json.dumps(args, sort_keys=True, default=str))
                if detect_cycles and sig in seen:
                    log.warning("cycle detected: repeated call to %s; short-circuiting", name)
                    result = {
                        "error": (
                            f"cycle: identical call to {name!r} was already made. "
                            "Do not repeat it. Either call a different tool or "
                            "produce your final answer now."
                        )
                    }
                else:
                    seen.add(sig)
                    result = bus.call(name, args)

                invocations.append({"name": name, "arguments": args, "result": result})
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        # max_turns exhausted while the model still wants tools: force a closing
        # answer with tools disabled so the caller always gets final text.
        log.warning("tool loop hit max_turns=%d; forcing a final answer", max_turns)
        resp, provider_name, resolved_model = self._call_providers(
            model=model, kwargs=base_kwargs(with_tools=False)
        )
        choice = resp.choices[0]
        total_in, total_out = self._add_usage(resp, total_in, total_out)
        telemetry.record_llm_call(
            name="llm.tool_loop", model=resolved_model, provider=provider_name,
            in_tokens=total_in, out_tokens=total_out,
            input_text=_last_user_text(messages), output_text=choice.message.content or "",
        )
        return ChatResult(
            text=choice.message.content or "",
            in_tokens=total_in,
            out_tokens=total_out,
            model=resolved_model,
            provider=provider_name,
            finish_reason=getattr(choice, "finish_reason", None),
            tool_turns=tool_turns,
            tool_results=invocations,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _assistant_to_dict(msg) -> dict:
        """Render an assistant message (possibly with tool calls) back to a dict.

        The dict is appended to the running conversation and re-sent on the next
        turn, so it must round-trip the ``tool_calls`` the model emitted.
        """
        out: dict = {"role": "assistant", "content": msg.content or ""}
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ]
        return out

    @staticmethod
    def _add_usage(resp, total_in: int, total_out: int) -> tuple[int, int]:
        usage = getattr(resp, "usage", None)
        total_in += getattr(usage, "prompt_tokens", 0) or 0
        total_out += getattr(usage, "completion_tokens", 0) or 0
        return total_in, total_out

    @staticmethod
    def _to_result(resp, *, provider: str, model: str) -> ChatResult:
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return ChatResult(
            text=choice.message.content or "",
            in_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            out_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=getattr(resp, "model", model) or model,
            provider=provider,
            finish_reason=getattr(choice, "finish_reason", None),
        )


_client: LLMClient | None = None


def get_client() -> LLMClient:
    """Return a process-wide :class:`LLMClient` singleton."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
