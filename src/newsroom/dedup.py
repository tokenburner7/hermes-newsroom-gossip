"""Deduplication pipeline (plan §4 Day 5).

Four complementary signals keep the newsroom from publishing the same story twice:

1. **URL hash** — exact-duplicate detection on the canonical ``url_hash``
   (``sha256(url)``), the cheapest and strongest check.
2. **Content simhash** — a 64-bit locality-sensitive fingerprint of the source
   text. Two near-identical documents differ in only a handful of bits, so a
   small Hamming distance flags reworded / cross-posted copies that the URL hash
   misses.
3. **Cosine near-dup** — semantic similarity between a draft body and existing
   articles via the pgvector HNSW index on ``articles.embedding`` (``<=>`` is
   cosine distance; embeddings are L2-normalized so ``1 - distance`` is cosine
   similarity).
4. **Title uniqueness** — a cheap Levenshtein guard so two articles never ship
   with near-identical headlines.

The 64-bit simhash is stored in ``sources.content_simhash``, a Postgres
``BIGINT`` (signed, range ``[-2^63, 2^63-1]``). A simhash is an *unsigned* 64-bit
value, so it is mapped to/from the signed two's-complement representation at the
storage boundary (:func:`_to_signed64` / :func:`_to_unsigned64`); Hamming
distance is always computed on the unsigned forms.

All database functions are ``async`` and use the shared async session factory;
the pure helpers (:func:`compute_simhash`, :func:`_levenshtein`) are synchronous.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import text

from .config import settings
from .db import async_session_factory
from .embedding import embed
from .sources._base import sha256_hex

# 64-bit simhash constants.
_BITS = 64
_MASK64 = (1 << _BITS) - 1
_SIGN_BIT = 1 << (_BITS - 1)  # 2**63
_WORD_RE = re.compile(r"[a-z0-9]+")


# --- Pure helpers ------------------------------------------------------------


def _tokenize(text_in: str) -> list[str]:
    """Lowercase ``text_in`` and split into alphanumeric word tokens."""
    return _WORD_RE.findall(text_in.lower())


def _hash64(token: str) -> int:
    """Hash ``token`` to a uniformly-distributed unsigned 64-bit integer."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def compute_simhash(text_in: str) -> int:
    """Return the 64-bit Charikar simhash of ``text_in`` as an unsigned int.

    Each distinct token is hashed to 64 bits and contributes a per-bit vote
    weighted by its frequency: ``+w`` where the token's bit is 1, ``-w`` where it
    is 0. The fingerprint's bit *i* is set when the column total is positive, so
    documents sharing most tokens land within a few bits of each other (small
    Hamming distance). Returns ``0`` for empty / tokenless input.
    """
    tokens = _tokenize(text_in)
    if not tokens:
        return 0

    columns = [0] * _BITS
    for token, weight in Counter(tokens).items():
        token_hash = _hash64(token)
        for i in range(_BITS):
            if (token_hash >> i) & 1:
                columns[i] += weight
            else:
                columns[i] -= weight

    fingerprint = 0
    for i in range(_BITS):
        if columns[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two unsigned 64-bit integers."""
    return ((a ^ b) & _MASK64).bit_count()


def _to_signed64(value: int) -> int:
    """Map an unsigned 64-bit int to its signed BIGINT two's-complement form."""
    value &= _MASK64
    return value - (1 << _BITS) if value & _SIGN_BIT else value


def _to_unsigned64(value: int) -> int:
    """Inverse of :func:`_to_signed64`: signed BIGINT back to unsigned 64-bit."""
    return value & _MASK64


def _levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance between two strings (iterative, O(len(a)*len(b)))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            )
        previous = current
    return previous[-1]


# --- Result type -------------------------------------------------------------


@dataclass(frozen=True)
class DedupResult:
    """Outcome of :func:`check_dedup`.

    Attributes
    ----------
    is_duplicate: whether ``text``/``url`` duplicates an existing source.
    duplicate_of: the matched ``sources.id``, or ``None``.
    method: which signal fired — ``"url_hash"``, ``"simhash"`` or ``"none"``.
    score: confidence in ``[0, 1]`` (``1.0`` exact URL match; simhash uses
        ``1 - hamming/64``; ``0.0`` when nothing matched).
    """

    is_duplicate: bool
    duplicate_of: int | None
    method: str
    score: float


# --- Database checks ---------------------------------------------------------


async def check_url_dup(url: str) -> int | None:
    """Return the ``sources.id`` whose ``url_hash`` matches ``url``, else None."""
    url_hash = sha256_hex(url)
    async with async_session_factory() as session:
        result = await session.execute(
            text("SELECT id FROM sources WHERE url_hash = :h ORDER BY id LIMIT 1"),
            {"h": url_hash},
        )
        return result.scalar_one_or_none()


async def _nearest_simhash(
    simhash_val: int, source_class: str
) -> tuple[int, int] | None:
    """Return ``(source_id, hamming_distance)`` for the closest stored simhash in
    ``source_class``, or ``None`` if no candidate is within the configured
    Hamming threshold. Distance is computed in Python on the unsigned forms.
    """
    threshold = settings.dedup_simhash_hamming_threshold
    async with async_session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT id, content_simhash FROM sources "
                "WHERE source_class = :sc AND content_simhash IS NOT NULL"
            ),
            {"sc": source_class},
        )
        best: tuple[int, int] | None = None
        for source_id, stored in rows:
            distance = hamming_distance(simhash_val, _to_unsigned64(stored))
            if distance < threshold and (best is None or distance < best[1]):
                best = (source_id, distance)
        return best


async def check_simhash_dup(
    simhash_val: int, source_class: str = "arxiv"
) -> int | None:
    """Return the ``sources.id`` of a near-duplicate (Hamming distance under the
    configured threshold) within ``source_class``, or ``None``.
    """
    match = await _nearest_simhash(simhash_val, source_class)
    return match[0] if match else None


async def _nearest_article(
    body_md: str, article_id: int | None = None
) -> tuple[int | None, float]:
    """Return ``(nearest_article_id, cosine_similarity)`` for ``body_md``.

    Embeds ``body_md`` and asks pgvector for the nearest neighbour by cosine
    distance (``<=>``), excluding ``article_id`` when given. Similarity is
    ``1 - distance`` in ``[0, 1]``; returns ``(None, 0.0)`` when there is no other
    embedded article to compare against.
    """
    if not body_md or not body_md.strip():
        return None, 0.0
    vector = embed([body_md])[0]
    vector_literal = "[" + ",".join(str(float(x)) for x in vector) + "]"
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, embedding <=> CAST(:vec AS vector) AS distance "
                    "FROM articles "
                    "WHERE embedding IS NOT NULL "
                    "AND (CAST(:exclude_id AS BIGINT) IS NULL OR id <> :exclude_id) "
                    "ORDER BY distance LIMIT 1"
                ),
                {"vec": vector_literal, "exclude_id": article_id},
            )
        ).first()
    if row is None or row[1] is None:
        return None, 0.0
    return int(row[0]), 1.0 - float(row[1])


async def near_dup_check(body_md: str, article_id: int | None = None) -> float:
    """Max cosine similarity between ``body_md`` and any *other* article.

    Returns ``1 - distance`` (cosine similarity in ``[0, 1]``), or ``0.0`` when
    there is no other embedded article. The near-dup *decision* — comparing this
    against the active (adaptive or static) threshold — lives in
    :func:`near_dup_verdict`.
    """
    _, similarity = await _nearest_article(body_md, article_id)
    return similarity


async def check_title_uniqueness(headline: str, exclude_id: int | None = None) -> dict:
    """Compare ``headline`` against every other article's headline.

    Returns a dict ``{is_unique, min_distance, closest_id, closest_headline}``.
    ``is_unique`` is False when the nearest headline is within
    ``settings.title_min_edit_distance`` Levenshtein edits.
    """
    async with async_session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT id, headline FROM articles "
                "WHERE (CAST(:exclude_id AS BIGINT) IS NULL OR id <> :exclude_id)"
            ),
            {"exclude_id": exclude_id},
        )
        candidates = rows.all()

    min_distance: int | None = None
    closest_id: int | None = None
    closest_headline: str | None = None
    for other_id, other_headline in candidates:
        distance = _levenshtein(headline, other_headline or "")
        if min_distance is None or distance < min_distance:
            min_distance, closest_id, closest_headline = (
                distance,
                other_id,
                other_headline,
            )

    is_unique = min_distance is None or min_distance >= settings.title_min_edit_distance
    return {
        "is_unique": is_unique,
        "min_distance": min_distance,
        "closest_id": closest_id,
        "closest_headline": closest_headline,
    }


async def store_simhash(source_id: int, simhash_val: int) -> None:
    """Persist ``simhash_val`` (unsigned 64-bit) into ``sources.content_simhash``."""
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE sources SET content_simhash = :sh WHERE id = :id"),
            {"sh": _to_signed64(simhash_val), "id": source_id},
        )
        await session.commit()


async def _load_source(source_id: int) -> dict | None:
    """Fetch ``(url, text, source_class)`` for ``source_id``, or None if absent.

    The simhash is computed over ``cleaned_text`` when present, else the title.
    """
    async with async_session_factory() as session:
        row = await session.execute(
            text(
                "SELECT url, source_class, cleaned_text, title "
                "FROM sources WHERE id = :id"
            ),
            {"id": source_id},
        )
        record = row.first()
    if record is None:
        return None
    url, source_class, cleaned_text, title = record
    return {
        "url": url,
        "source_class": source_class,
        "text": (cleaned_text or title or ""),
    }


async def check_dedup(source_id: int, text_in: str, url: str) -> DedupResult:
    """Run the cheap-to-expensive dedup chain for one source.

    1. **URL hash** — an exact ``url_hash`` collision with a *different* source is
       a hard duplicate (score ``1.0``).
    2. **Simhash** — otherwise fingerprint ``text_in`` and look for a near-dup in
       the same ``source_class``; score is ``1 - hamming/64``.

    Returns a :class:`DedupResult`; ``method == "none"`` when nothing matched.
    """
    url_match = await check_url_dup(url)
    if url_match is not None and url_match != source_id:
        return DedupResult(
            is_duplicate=True, duplicate_of=url_match, method="url_hash", score=1.0
        )

    source = await _load_source(source_id)
    source_class = source["source_class"] if source else "arxiv"

    simhash_val = compute_simhash(text_in)
    match = await _nearest_simhash(simhash_val, source_class)
    if match is not None and match[0] != source_id:
        matched_id, distance = match
        return DedupResult(
            is_duplicate=True,
            duplicate_of=matched_id,
            method="simhash",
            score=1.0 - distance / _BITS,
        )

    return DedupResult(
        is_duplicate=False, duplicate_of=None, method="none", score=0.0
    )


# --- Adaptive similarity threshold (Phase 3, O-M3) ---------------------------
#
# A fixed cosine cut-off (0.86) drifts as the corpus grows: new takes on a
# recurring beat start tripping the near-dup gate as false positives. The fix is
# to recompute the threshold from the corpus itself — the Nth percentile of the
# top-1 cosine similarity over recently-accepted articles — and store it in
# ``system_state['dedup_threshold']``, which :func:`get_dedup_threshold` reads.

#: system_state key holding the adaptive near-dup threshold.
DEDUP_THRESHOLD_KEY = "dedup_threshold"


@dataclass(frozen=True)
class ThresholdRecompute:
    """Outcome of :func:`recompute_threshold` (and the pure :func:`compute_adaptive_threshold`)."""

    old_threshold: float
    new_threshold: float
    n_samples: int
    #: The raw Nth-percentile of the samples *before* the cap, or ``None`` when
    #: there were too few samples to compute one.
    percentile_value: float | None
    #: True when ``new_threshold`` differs from ``old_threshold`` (i.e. stored).
    applied: bool
    #: True when the cap clamped the percentile down.
    capped: bool
    reason: str


@dataclass(frozen=True)
class NearDupVerdict:
    """Near-duplication decision against the *active* threshold."""

    is_near_dup: bool
    similarity: float
    threshold: float
    nearest_id: int | None


# --- pure logic --------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation ``q``-th percentile (``q`` in ``[0, 100]``).

    ``values`` need not be sorted. Raises :class:`ValueError` on empty input.
    """
    if not values:
        raise ValueError("percentile of an empty sequence")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    rank = (q / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def compute_adaptive_threshold(
    samples: list[float],
    current: float,
    *,
    min_samples: int | None = None,
    percentile: float | None = None,
    cap: float | None = None,
) -> ThresholdRecompute:
    """Pure: decide the new near-dup threshold from top-1 similarity ``samples``.

    Keeps ``current`` untouched when there are fewer than ``min_samples`` usable
    samples; otherwise adopts the ``percentile`` of ``samples``, clamped to
    ``cap``. No database access — split out so the statistics are unit-testable
    (mirrors :func:`newsroom.eval.drift.evaluate_distributions`).
    """
    min_samples = (
        settings.adaptive_threshold_min_samples if min_samples is None else min_samples
    )
    percentile = (
        settings.adaptive_threshold_percentile if percentile is None else percentile
    )
    cap = settings.adaptive_threshold_cap if cap is None else cap

    n = len(samples)
    if n < min_samples:
        return ThresholdRecompute(
            old_threshold=current,
            new_threshold=current,
            n_samples=n,
            percentile_value=None,
            applied=False,
            capped=False,
            reason=(
                f"insufficient data (have {n} sample(s), need ≥ {min_samples}) — "
                "keeping the current threshold"
            ),
        )

    raw = _percentile(samples, percentile)
    capped_val = min(raw, cap)
    new = round(capped_val, 4)
    was_capped = capped_val < raw
    return ThresholdRecompute(
        old_threshold=current,
        new_threshold=new,
        n_samples=n,
        percentile_value=round(raw, 4),
        applied=new != round(current, 4),
        capped=was_capped,
        reason=(
            f"p{percentile:g} of {n} top-1 similarities = {raw:.4f}"
            + (f" (capped at {cap:g})" if was_capped else "")
        ),
    )


# --- database seam -----------------------------------------------------------


async def get_dedup_threshold() -> float:
    """Active near-dup cosine threshold.

    Reads ``system_state['dedup_threshold']`` when adaptive thresholds are
    enabled and a value is stored; otherwise falls back to the static
    ``settings.dedup_similarity_threshold``. A malformed stored value also falls
    back to config rather than failing the gate.
    """
    if not settings.adaptive_threshold_enabled:
        return settings.dedup_similarity_threshold
    async with async_session_factory() as session:
        raw = (
            await session.execute(
                text("SELECT value FROM system_state WHERE key = :k"),
                {"k": DEDUP_THRESHOLD_KEY},
            )
        ).scalar_one_or_none()
    if raw is None:
        return settings.dedup_similarity_threshold
    try:
        return float(raw)
    except (TypeError, ValueError):
        return settings.dedup_similarity_threshold


async def set_dedup_threshold(value: float, reason: str = "") -> None:
    """Upsert the adaptive threshold into ``system_state['dedup_threshold']``."""
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO system_state (key, value, reason, updated_at)
                VALUES (:k, :v, :r, now())
                ON CONFLICT (key)
                DO UPDATE SET value = :v, reason = :r, updated_at = now()
                """
            ),
            {"k": DEDUP_THRESHOLD_KEY, "v": f"{float(value):.6f}", "r": reason or None},
        )
        await session.commit()


async def _load_top1_similarities(*, lookback_days: int | None = None) -> list[float]:
    """Top-1 cosine similarity (to any *other* article) per accepted article.

    "Accepted" = ``status = 'published'`` with an embedding, published within the
    lookback window. Articles with no other embedded article to compare against
    are skipped (their nearest-neighbour distance is NULL).
    """
    lookback_days = (
        settings.adaptive_threshold_lookback_days
        if lookback_days is None
        else lookback_days
    )
    async with async_session_factory() as session:
        rows = await session.execute(
            text(
                """
                SELECT (
                    SELECT MIN(a.embedding <=> b.embedding)
                    FROM articles b
                    WHERE b.embedding IS NOT NULL AND b.id <> a.id
                ) AS distance
                FROM articles a
                WHERE a.status = 'published'
                  AND a.embedding IS NOT NULL
                  AND a.published_at >= now() - make_interval(days => :days)
                """
            ),
            {"days": int(lookback_days)},
        )
        return [1.0 - float(d) for (d,) in rows if d is not None]


async def recompute_threshold(*, store: bool = True) -> ThresholdRecompute:
    """Recompute the adaptive near-dup threshold from the recent corpus (O-M3).

    The configured percentile (default 95th) of the top-1 cosine similarity over
    accepted articles in the lookback window, capped at
    ``adaptive_threshold_cap`` (0.95), replaces the stored threshold. With fewer
    than ``adaptive_threshold_min_samples`` usable samples the existing threshold
    is left untouched. Returns a :class:`ThresholdRecompute` either way; only a
    *changed* threshold is persisted (when ``store``).
    """
    current = await get_dedup_threshold()
    samples = await _load_top1_similarities()
    result = compute_adaptive_threshold(samples, current)
    if store and result.applied:
        await set_dedup_threshold(result.new_threshold, result.reason)
    return result


async def near_dup_verdict(
    body_md: str, article_id: int | None = None
) -> NearDupVerdict:
    """Decide whether ``body_md`` is a near-duplicate under the active threshold.

    Reads the threshold from ``system_state`` (adaptive) with the config value as
    fallback, so the gate tracks the corpus once :func:`recompute_threshold` has
    run. ``article_id`` excludes a row from the comparison (e.g. re-checking an
    already-stored article against the rest of the corpus).
    """
    nearest_id, similarity = await _nearest_article(body_md, article_id)
    threshold = await get_dedup_threshold()
    return NearDupVerdict(
        is_near_dup=similarity >= threshold,
        similarity=similarity,
        threshold=threshold,
        nearest_id=nearest_id,
    )
