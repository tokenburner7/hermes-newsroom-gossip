"""Article embeddings (plan §4 Day 5, O-M1 fix).

Embeds text with **BAAI/bge-base-en-v1.5** (768-dim) via sentence-transformers and
writes the vector into ``articles.embedding`` (a ``VECTOR(768)`` column matching
``settings.embedding_dim``). The model is heavy to load, so it is lazy-loaded on the
first :func:`embed` call and cached process-wide.

Naming note: this module is ``embedding.py`` (singular) — the canonical name the
rest of the pipeline imports. There is no ``embeddings.py``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .config import settings
from .db import get_sync_session_factory
from .models import Article

log = logging.getLogger(__name__)

# Lazily-loaded SentenceTransformer; populated on first embed().
_model = None


def _get_model():
    """Load (once) and return the sentence-transformers model named in settings."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s (first use)", settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` into unit-normalized 768-dim vectors.

    Returns one ``list[float]`` per input string, in order. Embeddings are
    L2-normalized so a dot product equals cosine similarity (matching the HNSW
    ``vector_cosine_ops`` index on ``articles.embedding``).
    """
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    out = [v.tolist() for v in vectors]
    dim = len(out[0]) if out else 0
    if dim and dim != settings.embedding_dim:
        # Surface a config/model mismatch loudly — a wrong-dim vector otherwise
        # fails the VECTOR(768) insert with a confusing low-level error.
        raise ValueError(
            f"embedding dim {dim} != configured {settings.embedding_dim}; "
            f"check EMBEDDING_MODEL ({settings.embedding_model})"
        )
    return out


def article_text(article: Article) -> str:
    """Build the text we embed for an article: headline + dek + body."""
    parts = [article.headline or "", article.dek or "", article.body_md or ""]
    return "\n\n".join(p for p in parts if p).strip()


def embed_article(article_id: int) -> None:
    """Embed one article (headline + dek + body) and persist the vector in place.

    Raises ``LookupError`` if the article does not exist.
    """
    factory = get_sync_session_factory()
    with factory() as session:
        article = session.execute(
            select(Article).where(Article.id == article_id)
        ).scalar_one_or_none()
        if article is None:
            raise LookupError(f"no article with id {article_id}")
        article.embedding = embed([article_text(article)])[0]
        session.commit()
    log.info("embedded article id=%d (%d-dim)", article_id, settings.embedding_dim)
