"""X / Twitter gossip-account ingestion source (no RSS).

Pulls recent posts from a handful of entertainment-scoop accounts via the user's
``xurl`` CLI (an OAuth-aware curl for the X API). X exposes no public RSS, so we
shell out to ``xurl timeline <account> --count 20`` and normalize the returned
JSON. The subprocess call runs off the event loop via ``asyncio.to_thread``. If
``xurl`` is not on PATH the source degrades to a no-op rather than failing the
ingest run, and any per-account error is logged and skipped.

  external_id = tweet id (falls back to a hash of the account + text)
  cleaned_text = full tweet text
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess

from ._base import SourceItem, sha256_hex, upsert_items

SOURCE_CLASS = "x_gossip"

log = logging.getLogger(__name__)

ACCOUNTS = ["PopCrave", "PopBase", "DiscussingFilm", "FilmUpdates"]

TWEETS_PER_ACCOUNT = 20
SUBPROCESS_TIMEOUT_S = 30.0
TITLE_CHARS = 100


def _tweet_url(account: str, tweet_id: str) -> str:
    return f"https://x.com/{account}/status/{tweet_id}"


def _iter_tweets(payload: object):
    """Yield tweet dicts from xurl JSON, tolerating a few common shapes."""
    if isinstance(payload, list):
        for tweet in payload:
            if isinstance(tweet, dict):
                yield tweet
        return
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            for tweet in data:
                if isinstance(tweet, dict):
                    yield tweet
        elif isinstance(data, dict):
            yield data


def _normalize_account(account: str, payload: object) -> list[SourceItem]:
    items: list[SourceItem] = []
    for tweet in _iter_tweets(payload):
        text = (tweet.get("text") or tweet.get("full_text") or "").strip()
        if not text:
            continue
        tweet_id = str(tweet.get("id") or tweet.get("id_str") or "").strip()
        if not tweet_id:
            tweet_id = sha256_hex(f"{account}:{text}")[:16]
        items.append(
            SourceItem(
                external_id=tweet_id,
                url=_tweet_url(account, tweet_id),
                title=text[:TITLE_CHARS],
                cleaned_text=text,
                published_at=None,
                categories=[SOURCE_CLASS, f"@{account}"],
            )
        )
    return items


def _run_xurl(account: str) -> str | None:
    """Run ``xurl timeline <account> --count N``; return stdout or None."""
    try:
        proc = subprocess.run(
            ["xurl", "timeline", account, "--count", str(TWEETS_PER_ACCOUNT)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("x_gossip: xurl invocation failed for @%s: %s", account, exc)
        return None
    if proc.returncode != 0:
        log.warning(
            "x_gossip: xurl exited %d for @%s: %s",
            proc.returncode,
            account,
            (proc.stderr or "").strip()[:200],
        )
        return None
    return proc.stdout


async def _fetch_account(account: str) -> list[SourceItem]:
    raw = await asyncio.to_thread(_run_xurl, account)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log.warning("x_gossip: could not parse xurl JSON for @%s: %s", account, exc)
        return []
    return _normalize_account(account, payload)


async def fetch() -> list[SourceItem]:
    """Pull recent tweets per account via xurl; [] if xurl is unavailable."""
    if shutil.which("xurl") is None:
        log.warning("x_gossip: xurl CLI not found on PATH; skipping")
        return []
    items: list[SourceItem] = []
    for account in ACCOUNTS:
        items.extend(await _fetch_account(account))
    return items


async def ingest() -> tuple[int, int]:
    """Fetch X gossip-account posts and upsert. Returns ``(fetched, upserted)``."""
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
