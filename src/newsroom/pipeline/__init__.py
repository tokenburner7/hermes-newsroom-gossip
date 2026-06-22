"""Pipeline stages (plan §3.1): research → draft → gate → escalate → fact-gate → humanize → publish.

Each stage is a pure-ish function over a typed payload (plan §3.1). Phase 0 wired
research → draft → factcheck → embed → publish behind direct calls; Phase 1 Week 2
adds the gate judge + model escalation and the verified humanizer. Phase 2 swaps
these for Temporal activities with no signature changes.
"""

from __future__ import annotations

from .draft import LABEL_AUTO_GATED, ArticleEnvelope, draft, persist_article
from .escalation import EscalationResult, escalate_if_needed
from .factcheck import (
    PASS_THRESHOLD,
    ClaimResult,
    FactCheckResult,
    fact_check,
    span_score,
)
from .humanize import (
    HumanizeVerifyResult,
    humanize,
    humanize_detailed,
    record_humanize,
    verify_humanize,
)
from .publish import (
    CONTENT_DIR,
    CROSSLINK_BEGIN,
    CROSSLINK_END,
    PublishResult,
    publish,
    republish,
)
from .research import ResearchResult, record_run_error, research

__all__ = [
    # research
    "research",
    "ResearchResult",
    "record_run_error",
    # draft
    "draft",
    "ArticleEnvelope",
    "persist_article",
    "LABEL_AUTO_GATED",
    # escalation
    "escalate_if_needed",
    "EscalationResult",
    # factcheck
    "fact_check",
    "FactCheckResult",
    "ClaimResult",
    "span_score",
    "PASS_THRESHOLD",
    # humanize
    "humanize",
    "humanize_detailed",
    "verify_humanize",
    "record_humanize",
    "HumanizeVerifyResult",
    # publish
    "publish",
    "republish",
    "PublishResult",
    "CONTENT_DIR",
    "CROSSLINK_BEGIN",
    "CROSSLINK_END",
]
