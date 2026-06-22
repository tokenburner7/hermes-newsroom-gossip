"""OpenTelemetry tracing + optional Langfuse LLM tracing (plan §6, Phase 2).

A single-file observability seam. It provides:

* a process-wide :class:`~opentelemetry.sdk.trace.TracerProvider` exporting to a
  local OTLP collector when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, otherwise to
  the console (Phase-2 dev default — spans go to *stderr* so they never mingle
  with the rich stdout tables);
* :func:`traced` — a decorator that wraps a pipeline stage in a span, stamps the
  common attributes (``run_id``, ``article_type``, ``source_id``, and, post-hoc,
  ``tokens_in``/``tokens_out`` from the result), and records exceptions;
* :func:`span` — a context manager for ad-hoc spans;
* :func:`record_llm_call` — adds an ``llm.call`` event to the current span and
  emits a Langfuse *generation* (prompt/response/model/tokens/cost/latency),
  linked to the OpenTelemetry trace via the shared tracer provider;
* :func:`current_trace_id` — the active trace id (hex), shown by ``run-once``.

Everything degrades to a no-op when OpenTelemetry is not installed or
``OTEL_ENABLED`` is false, so importing ``newsroom`` never hard-depends on the
telemetry stack. Langfuse is fully optional: with no keys (or the package
absent) the LLM-trace path is a clearly-commented stub.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import inspect
import logging
import sys
from collections.abc import Iterator
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

# --- span attribute keys (single namespace so they group in any backend) -----
ATTR_RUN_ID = "newsroom.run_id"
ATTR_ARTICLE_TYPE = "newsroom.article_type"
ATTR_SOURCE_ID = "newsroom.source_id"
ATTR_SOURCE_CLASS = "newsroom.source_class"
ATTR_TOKENS_IN = "newsroom.tokens_in"
ATTR_TOKENS_OUT = "newsroom.tokens_out"
ATTR_COST_USD = "newsroom.cost_usd"
ATTR_MODEL = "newsroom.model"
ATTR_PROVIDER = "newsroom.provider"

# Map a keyword/parameter name -> the span attribute it sets. Lets both the
# decorator (binding stage args) and :func:`set_attributes` share one mapping.
_ATTR_MAP: dict[str, str] = {
    "run_id": ATTR_RUN_ID,
    "article_type": ATTR_ARTICLE_TYPE,
    "art_type": ATTR_ARTICLE_TYPE,
    "source_id": ATTR_SOURCE_ID,
    "source_ids": ATTR_SOURCE_ID,
    "source_class": ATTR_SOURCE_CLASS,
    "name": ATTR_SOURCE_CLASS,  # ingest_source(name=...) — name is the source class
    "tokens_in": ATTR_TOKENS_IN,
    "in_tokens": ATTR_TOKENS_IN,
    "tokens_out": ATTR_TOKENS_OUT,
    "out_tokens": ATTR_TOKENS_OUT,
    "cost_usd": ATTR_COST_USD,
    "model": ATTR_MODEL,
    "provider": ATTR_PROVIDER,
}

# --- soft dependency on OpenTelemetry ----------------------------------------
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when otel is uninstalled
    _OTEL_AVAILABLE = False
    log.warning("opentelemetry not installed; tracing is a no-op")


# --- module state ------------------------------------------------------------
_provider = None
_tracer = None
_langfuse = None
_initialized = False


def init_telemetry() -> None:
    """Build the tracer provider + exporter once (idempotent).

    No-ops if OpenTelemetry is unavailable or ``OTEL_ENABLED`` is false. Safe to
    call from any entry point; :func:`get_tracer` calls it lazily.
    """
    global _provider, _tracer, _initialized
    if _initialized:
        return
    _initialized = True

    if not (_OTEL_AVAILABLE and settings.otel_enabled):
        log.info(
            "telemetry disabled (otel_enabled=%s, available=%s)",
            settings.otel_enabled, _OTEL_AVAILABLE,
        )
        return

    resource = Resource.create({"service.name": settings.otel_service_name})
    _provider = TracerProvider(resource=resource)
    label, processor = _build_processor()
    if processor is not None:
        _provider.add_span_processor(processor)
    _otel_trace.set_tracer_provider(_provider)
    _tracer = _provider.get_tracer("newsroom")
    _maybe_init_langfuse(_provider)
    log.info("telemetry initialised (exporter=%s)", label)


def _build_processor():
    """Pick a span processor: OTLP if an endpoint is set, else console (stderr)."""
    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            return f"otlp:{endpoint}", BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint)
            )
        except Exception as exc:  # noqa: BLE001 - fall back to console on any failure
            log.warning("OTLP exporter init failed (%s); using console exporter", exc)

    if settings.otel_console_export:
        # SimpleSpanProcessor flushes synchronously (ordered, immediate) which is
        # ideal for a low-volume CLI; stderr keeps stdout tables clean.
        return "console", SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr))

    return "none", None


def get_tracer():
    """Return the process tracer (initialising lazily), or ``None`` if disabled."""
    if not _initialized:
        init_telemetry()
    return _tracer


# --- attribute helpers -------------------------------------------------------

def set_attributes(span, /, **values: Any) -> None:
    """Set mapped attributes on ``span`` (skipping ``None`` and unknown keys)."""
    if span is None:
        return
    for key, value in values.items():
        attr = _ATTR_MAP.get(key)
        if attr is None or value is None:
            continue
        try:
            span.set_attribute(attr, _coerce(value))
        except Exception:  # noqa: BLE001 - attribute setting must never break a stage
            pass


def set_current_attributes(**values: Any) -> None:
    """Set attributes on the currently-active span, if any."""
    if not _OTEL_AVAILABLE:
        return
    span = _otel_trace.get_current_span()
    if span is not None and span.get_span_context().is_valid:
        set_attributes(span, **values)


def _coerce(value: Any):
    """OTel attributes accept str/bool/int/float (or homogeneous sequences)."""
    if isinstance(value, bool) or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def current_trace_id() -> str | None:
    """Return the active trace id as a 32-char hex string, or ``None``."""
    if not _OTEL_AVAILABLE:
        return None
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def _record_exception(span, exc: BaseException) -> None:
    try:
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception:  # noqa: BLE001
        pass


def _stamp_result(span, result: Any) -> None:
    """Post-hoc stamp ``tokens_in``/``tokens_out`` from a stage result if present."""
    ti = getattr(result, "in_tokens", None)
    to = getattr(result, "out_tokens", None)
    if ti is not None or to is not None:
        set_attributes(span, in_tokens=ti or 0, out_tokens=to or 0)


# --- the @traced decorator ---------------------------------------------------

def traced(name: str | None = None, **static_attrs: Any):
    """Wrap a pipeline stage in a span named ``name`` (default: the func name).

    Binds the call's arguments and stamps any that map to a span attribute
    (``run_id``, ``article_type``/``art_type``, ``source_id``/``source_ids``,
    ``name`` as source class). After the call, token counts are read off the
    return value when available. Exceptions are recorded and re-raised. Works on
    both sync and async functions; a no-op wrapper is used when tracing is off.
    """

    def decorator(func):
        span_name = name or func.__name__
        sig = inspect.signature(func)

        def _bound_attrs(args, kwargs) -> dict:
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                return {k: v for k, v in bound.arguments.items() if k in _ATTR_MAP}
            except Exception:  # noqa: BLE001 - never let attribute binding break a call
                return {}

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                tracer = get_tracer()
                if tracer is None:
                    return await func(*args, **kwargs)
                with tracer.start_as_current_span(span_name) as span:
                    set_attributes(span, **static_attrs)
                    set_attributes(span, **_bound_attrs(args, kwargs))
                    try:
                        result = await func(*args, **kwargs)
                    except Exception as exc:
                        _record_exception(span, exc)
                        raise
                    _stamp_result(span, result)
                    return result

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tracer = get_tracer()
            if tracer is None:
                return func(*args, **kwargs)
            with tracer.start_as_current_span(span_name) as span:
                set_attributes(span, **static_attrs)
                set_attributes(span, **_bound_attrs(args, kwargs))
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    _record_exception(span, exc)
                    raise
                _stamp_result(span, result)
                return result

        return wrapper

    return decorator


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Context manager for an ad-hoc span (yields the span, or ``None`` if off)."""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as sp:
        set_attributes(sp, **attrs)
        try:
            yield sp
        except Exception as exc:
            _record_exception(sp, exc)
            raise


#: Alias used at the top-level CLI entry to make the intent explicit.
root_span = span


# --- LLM call tracing (OTel event + optional Langfuse generation) ------------

_LLM_TEXT_CLIP = 2000


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= _LLM_TEXT_CLIP else text[:_LLM_TEXT_CLIP] + " …[clipped]"


def record_llm_call(
    *,
    model: str = "",
    provider: str = "",
    in_tokens: int = 0,
    out_tokens: int = 0,
    cost_usd: float | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
    name: str = "llm.chat",
) -> None:
    """Record one LLM call: an event on the current span + a Langfuse generation.

    Adds an ``llm.call`` event (so several calls within one stage are all
    visible) and emits a Langfuse *generation* observation when Langfuse is
    configured. Both are best-effort and never raise.
    """
    if _OTEL_AVAILABLE:
        sp = _otel_trace.get_current_span()
        if sp is not None and sp.get_span_context().is_valid:
            attrs: dict[str, Any] = {
                ATTR_MODEL: model or "",
                ATTR_PROVIDER: provider or "",
                ATTR_TOKENS_IN: int(in_tokens or 0),
                ATTR_TOKENS_OUT: int(out_tokens or 0),
            }
            if cost_usd is not None:
                attrs[ATTR_COST_USD] = float(cost_usd)
            try:
                sp.add_event(name, attributes=attrs)
            except Exception:  # noqa: BLE001
                pass

    _langfuse_generation(
        name=name,
        model=model,
        provider=provider,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        cost_usd=cost_usd,
        input_text=input_text,
        output_text=output_text,
    )


# --- Langfuse (optional, OTel-native in v3/v4) -------------------------------

def _maybe_init_langfuse(provider) -> None:
    """Construct the Langfuse client, sharing our tracer provider for linkage.

    Sharing the provider means each Langfuse generation nests under the active
    OpenTelemetry span (the requested "link traces to the OTel span"). No keys /
    package / disabled => left as ``None`` and every call below is a no-op stub.
    """
    global _langfuse
    if not settings.langfuse_enabled:
        return
    if not settings.langfuse_configured:
        log.info("langfuse enabled but keys missing; LLM tracing stays a no-op stub")
        return
    try:
        from langfuse import Langfuse
    except ImportError:  # pragma: no cover - package optional
        log.info("langfuse package not installed; LLM tracing is a no-op stub")
        return
    try:
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            tracer_provider=provider,
        )
        log.info("langfuse LLM tracing enabled (host=%s)", settings.langfuse_host)
    except Exception as exc:  # noqa: BLE001 - never let observability break startup
        log.warning("langfuse init failed; continuing without LLM tracing: %s", exc)
        _langfuse = None


def _langfuse_generation(
    *, name, model, provider, in_tokens, out_tokens, cost_usd, input_text, output_text
) -> None:
    if _langfuse is None:
        return
    try:
        generation = _langfuse.start_observation(
            name=name,
            as_type="generation",
            model=model or None,
            input=_clip(input_text),
            output=_clip(output_text),
            usage_details={"input": int(in_tokens or 0), "output": int(out_tokens or 0)},
            cost_details=({"total": float(cost_usd)} if cost_usd is not None else None),
            metadata={"provider": provider} if provider else None,
        )
        generation.end()
    except Exception as exc:  # noqa: BLE001 - LLM tracing is best-effort
        log.debug("langfuse generation failed: %s", exc)


# --- shutdown ----------------------------------------------------------------

def shutdown_telemetry() -> None:
    """Flush Langfuse + the span processor. Registered with :mod:`atexit`."""
    global _langfuse
    if _langfuse is not None:
        with contextlib.suppress(Exception):
            _langfuse.flush()
    if _provider is not None:
        with contextlib.suppress(Exception):
            _provider.shutdown()


atexit.register(shutdown_telemetry)
