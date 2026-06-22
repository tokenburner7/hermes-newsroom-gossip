"""Phase-0 source selection for gossip vertical.

All sources from the requested classes pass through — ranked by a combination
of source weight (gossip-credibility tuned), recency, and a keyword-based
gossip-relevance score. The old crypto/arXiv filter is retired; gossip sources
don't have arXiv categories or crypto keywords.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select as sa_select

from .db import async_session_factory
from .models import Source

# ── Gossip-relevance scoring ──────────────────────────────────────────────

# Celebrity first + last names for keyword matching (case-insensitive).
# A/B/C-list — broad enough to catch mentions across all gossip verticals.
CELEBRITY_NAMES: list[str] = [
    # A-list
    "kanye", "ye", "kim", "kardashian", "taylor", "swift", "beyonce", "jay-z",
    "jayz", "rihanna", "drake", "kendrick", "lamar", "adele", "ed", "sheeran",
    "selena", "gomez", "justin", "bieber", "hailey", "baldwin", "ariana",
    "grande", "billie", "eilish", "harry", "styles", "zendaya", "tom", "holland",
    "timothée", "chalamet", "timothee", "dua", "lipa", "olivia", "rodrigo",
    "bad", "bunny", "kylie", "jenner", "kendall", "khloe", "kourtney",
    "travis", "scott", "travis", "kelce", "gigi", "hadid", "bella",
    "blake", "lively", "ryan", "reynolds", "margot", "robbie", "chris",
    "evans", "scarlett", "johansson", "robert", "downey", "angelina", "jolie",
    "brad", "pitt", "jennifer", "aniston", "jennifer", "lawrence", "emma",
    "stone", "ryan", "gosling", "leonardo", "dicaprio", "lady", "gaga",
    "miley", "cyrus", "the", "weeknd", "sabrina", "carpenter",
    # B-list / gossip staples
    "pete", "davidson", "machine", "gun", "kelly", "mgk", "megan", "fox",
    "cardan", "offset", "cardi", "nicki", "minaj", "doja", "cat",
    "chrishell", "simo", "giannina", "lala", "scheana", "ariana", "madix",
    "tom", "sandoval", "raquel", "leviss", "kyle", "richards", "sutton",
    "erika", "jayne", "lisa", "rinna", "ramona", "bethenny", "frankel",
    "charlie", "damelio", "dixie", "addison", "rae", "bryce", "hall",
    "alix", "earle", "james", "charles", "tana", "mongeau",
    # Music / industry
    "playboi", "carti", "frank", "ocean", "sza", "tyler", "creator",
    "pharrell", "justin", "timberlake", "britney", "spears", "christina",
    "aguilera", "shakira", "pitbull", "daddy", "yankee", "karol",
    "karol", "g", "becky", "g", "j", "balvin", "maluma", "ozuna",
    "rosalía", "rosalia",
]

# Scandal/drama keywords that boost gossip relevance.
GOSSIP_KEYWORDS: list[str] = [
    "divorce", "breakup", "break-up", "split", "separated", "separation",
    "feud", "fight", "fighting", "cheating", "cheat", "affair", "mistress",
    "arrested", "arrest", "jail", "prison", "charges", "lawsuit", "sued",
    "fired", "walked off", "stormed out", "quit", "resigned",
    "blindsided", "secret", "exclusive", "leaked", "leak", "revealed",
    "brawl", "fight", "nude", "naked", "topless", "wardrobe malfunction",
    "shocking", "shock", "busted", "caught", "exposed", "scandal",
    "spotted", "sighting", "seen with", "dating", "dating rumors",
    "engaged", "engagement", "pregnant", "pregnancy", "baby", "babies",
    "wedding", "married", "marriage", "honeymoon",
    "rehab", "overdose", "drugs", "relapse", "sober", "sobriety",
    "million", "billion", "deal", "contract", "signed", "signing",
    "number one", "#1", "records", "record-breaking", "box office",
    "broke up", "dumped", "ghosted", "third wheel", "love triangle",
    "revenge", "comeback", "apology", "apologized", "cancelled", "canceled",
    "dragged", "clapped back", "clapback", "shade", "threw shade",
    "viral", "trending", "blowing up",
]

# Source-class weights: higher = more tabloid/gossip energy.
# Trade publications (Variety, Deadline) are credible but less juicy.
SOURCE_CLASS_WEIGHTS: dict[str, float] = {
    "tmz": 0.95,
    "pagesix": 0.90,
    "usweekly": 0.85,
    "justjared": 0.80,
    "eonline": 0.75,
    "buzzfeed": 0.70,
    "x_gossip": 0.68,
    "reddit": 0.55,
    "thewrap": 0.45,
    "variety": 0.40,
    "deadline": 0.40,
}

_GOSSIP_RE = re.compile(
    "|".join(re.escape(w) for w in GOSSIP_KEYWORDS), re.IGNORECASE
)
_CELEB_RE = re.compile(
    "|".join(re.escape(n) for n in CELEBRITY_NAMES), re.IGNORECASE
)


def _gossip_score(title: str) -> float:
    """Score a source title/description for gossip relevance (0.0–1.0)."""
    if not title:
        return 0.0
    text = title.lower()
    keyword_hits = len(set(_GOSSIP_RE.findall(text)))
    celeb_hits = len(set(_CELEB_RE.findall(text)))
    # Keyword hits saturated at 5, celeb hits at 3
    kw_score = min(keyword_hits / 5.0, 1.0)
    celeb_score = min(celeb_hits / 3.0, 1.0)
    # Keywords weighted more than celeb names (a scandal without a name is still gossip)
    return round(kw_score * 0.55 + celeb_score * 0.45, 3)


def _source_class_weight(source_class: str | None) -> float:
    """Return the gossip weight for a source class (default 0.5)."""
    if not source_class:
        return 0.5
    return SOURCE_CLASS_WEIGHTS.get(source_class, 0.5)


@dataclass(slots=True)
class SelectionResult:
    """One source ready for research, with ranking info."""

    source_id: int
    external_id: str
    title: str
    url: str
    score: float
    gossip_score: float
    category_match: bool
    keyword_hits: list[str]


async def select_sources(
    *, source_classes: list[str] | None = None, limit: int | None = None
) -> list[SelectionResult]:
    """Return sources from the requested classes, ranked by gossip relevance.

    Every ingested source passes — no hard content filter. Sources are scored
    on a combination of source-class credibility weight, gossip-keyword hits,
    and celebrity-name matches. Recency is used as a tiebreaker.
    """
    stmt = sa_select(Source)
    if source_classes:
        stmt = stmt.where(Source.source_class.in_(source_classes))
    async with async_session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()

    results: list[SelectionResult] = []
    for src in rows:
        title = (src.title or "").strip()
        gs = _gossip_score(title)
        class_w = _source_class_weight(src.source_class)
        # Composite: base weight (0.3) + gossip score (0.35) + class weight (0.35)
        composite = round(
            (src.weight if src.weight is not None else 0.5) * 0.30
            + gs * 0.35
            + class_w * 0.35,
            3,
        )
        results.append(
            SelectionResult(
                source_id=src.id,
                external_id=src.external_id,
                title=title,
                url=src.url,
                score=composite,
                gossip_score=gs,
                category_match=False,
                keyword_hits=list(set(_GOSSIP_RE.findall(title.lower()))),
            )
        )

    # Sort by composite score, then by recency (newest first) as tiebreaker
    results.sort(key=lambda r: (r.score, r.source_id), reverse=True)
    if limit is not None:
        results = results[:limit]
    return results


async def selected_source_ids(
    *, source_classes: list[str] | None = None, limit: int | None = None
) -> list[int]:
    """Return just the ranked ``source_id``s."""
    return [
        r.source_id
        for r in await select_sources(source_classes=source_classes, limit=limit)
    ]
