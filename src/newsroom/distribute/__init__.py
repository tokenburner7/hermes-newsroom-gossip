"""Distribution stage: repackage a published article into channel-native products.

The thread/post IS the product, not a link. Generation is decoupled from posting:
these functions produce + persist payloads (cheap LLM); the operator/agent posts
via post_thread.py (X) and the telegram skill and records the URL back onto the row.
"""

from __future__ import annotations

from .repackage import (
    ArticleContext,
    DistributeResult,
    TelegramResult,
    ThreadResult,
    distribute_article,
    distribute_latest,
    generate_telegram,
    generate_x_thread,
    latest_published_article_id,
    load_article_context,
)

__all__ = [
    "ArticleContext",
    "DistributeResult",
    "ThreadResult",
    "TelegramResult",
    "load_article_context",
    "latest_published_article_id",
    "generate_x_thread",
    "generate_telegram",
    "distribute_article",
    "distribute_latest",
]
