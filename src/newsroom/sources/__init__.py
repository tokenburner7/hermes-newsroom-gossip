"""Ingestion source registry.

Each source module exposes ``SOURCE_CLASS`` and an ``async def ingest()`` that
fetches, normalizes and upserts into the ``sources`` table, returning
``(fetched, upserted)``. The orchestrator (:mod:`newsroom.ingest`) drives them
through the :data:`SOURCES` mapping.

Note: ``arxiv.ingest`` takes a required look-back ``since`` argument (it predates
this registry); the orchestrator special-cases that signature.
"""

from __future__ import annotations

from types import ModuleType

from . import (
    arxiv,
    bls,
    bluesky,
    buzzfeed,
    coingecko,
    deadline,
    eonline,
    fred,
    gdelt,
    hackernews,
    justjared,
    pagesix,
    polymarket,
    reddit_rss,
    sec,
    thewrap,
    tmz,
    treasury,
    usweekly,
    variety,
    x_gossip,
)

# Public name -> source module. Keys are the canonical ``source_class`` values
# (note ``reddit`` maps to the ``reddit_rss`` module).
SOURCES: dict[str, ModuleType] = {
    "arxiv": arxiv,
    "bls": bls,
    "bluesky": bluesky,
    "buzzfeed": buzzfeed,
    "coingecko": coingecko,
    "deadline": deadline,
    "eonline": eonline,
    "fred": fred,
    "gdelt": gdelt,
    "hackernews": hackernews,
    "justjared": justjared,
    "pagesix": pagesix,
    "polymarket": polymarket,
    "reddit": reddit_rss,
    "sec": sec,
    "thewrap": thewrap,
    "tmz": tmz,
    "treasury": treasury,
    "usweekly": usweekly,
    "variety": variety,
    "x_gossip": x_gossip,
}

__all__ = ["SOURCES"]
