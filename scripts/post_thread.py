#!/usr/bin/env python3
"""Post a stored X-thread distribution as a reply chain via the `xurl` CLI.

Reads one `distributions` row (channel='x'), posts the selected hook variant + the
8 body tweets + the auto-generated closing tweet as a connected reply chain, then
records status='posted' + external_url + posted_at. One command replaces ~10 manual
xurl calls. This script ONLY posts already-generated payloads; it never calls the LLM.

Usage:
  uv run python scripts/post_thread.py --latest --hook A
  uv run python scripts/post_thread.py --distribution-id 42 --hook B
  uv run python scripts/post_thread.py --latest --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import select

# Make `newsroom` importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from newsroom.config import settings  # noqa: E402
from newsroom.db import get_sync_session_factory  # noqa: E402
from newsroom.models import Distribution  # noqa: E402

#: The xurl binary (the authorized X tool). Override with XURL_BIN if needed.
XURL_BIN = os.environ.get("XURL_BIN", "xurl")
#: Seconds between tweets — gentle pacing so the chain threads cleanly.
INTER_TWEET_DELAY = 2.0
HOOK_INDEX = {"A": 0, "B": 1, "C": 2}


def _xurl_post_tweet(text_body: str, reply_to: str | None) -> str:
    """POST one tweet via xurl; return the new tweet id. Raises on any failure."""
    payload: dict = {"text": text_body}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}
    proc = subprocess.run(
        [XURL_BIN, "-X", "POST", "/2/tweets", "-d", json.dumps(payload)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"xurl exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xurl returned non-JSON: {proc.stdout[:200]}") from exc
    tweet_id = (data.get("data") or {}).get("id")
    if not tweet_id:
        raise RuntimeError(f"xurl response missing data.id: {proc.stdout[:200]}")
    return str(tweet_id)


def _load_row(session, distribution_id: int | None):
    if distribution_id is not None:
        row = session.get(Distribution, distribution_id)
        if row is None:
            raise SystemExit(f"no distribution with id {distribution_id}")
    else:
        row = session.execute(
            select(Distribution)
            .where(
                Distribution.channel == "x",
                Distribution.status.in_(("generated", "partial")),
            )
            .order_by(Distribution.id.desc())
        ).scalars().first()
        if row is None:
            raise SystemExit("no generated X distribution to post (run `newsroom distribute` first)")
    if row.channel != "x":
        raise SystemExit(f"distribution {row.id} is channel={row.channel!r}, not 'x'")
    return row


def _tweets_from_payload(payload: dict, hook_letter: str) -> list[str]:
    hooks = payload.get("hooks") or []
    idx = HOOK_INDEX.get(hook_letter.upper(), 0)
    hook = hooks[idx] if idx < len(hooks) else (hooks[0] if hooks else "")
    body = payload.get("body_tweets") or []
    closing = payload.get("closing") or ""
    tweets = [hook, *body, closing]
    return [t.strip() for t in tweets if t and t.strip()]


def _thread_url(first_id: str) -> str:
    handle = settings.x_handle.lstrip("@")
    if not handle:
        return f"https://x.com/i/status/{first_id}"  # fallback: numeric URL
    return f"https://x.com/{handle}/status/{first_id}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Post a stored X-thread distribution via xurl.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--distribution-id", type=int, help="distributions.id (channel='x').")
    g.add_argument("--latest", action="store_true", help="Most recent generated X distribution.")
    ap.add_argument("--hook", default="A", choices=list(HOOK_INDEX), help="A/B/C hook variant.")
    ap.add_argument("--dry-run", action="store_true", help="Print tweets; do not post.")
    args = ap.parse_args()

    factory = get_sync_session_factory()
    with factory() as session:
        row = _load_row(session, None if args.latest else args.distribution_id)
        if row.status == "posted":
            raise SystemExit(f"distribution {row.id} already posted: {row.external_url}")
        payload = row.payload_json or {}
        tweets = _tweets_from_payload(payload, args.hook)
        if not tweets:
            raise SystemExit(f"distribution {row.id} has no tweets in payload_json")

        # Resume support: a 'partial' row records the tweet ids it already posted, so
        # we continue the live chain from the last one instead of re-posting tweets
        # that are already public (H3). The hook only ever sits at index 0, so a
        # resume is safe even if --hook differs from the original attempt.
        posted_ids: list[str] = [str(x) for x in (payload.get("posted_ids") or [])]
        resume_from = len(posted_ids)
        if resume_from >= len(tweets):
            raise SystemExit(
                f"distribution {row.id} already has {resume_from} posted tweets; "
                "nothing left to post (mark it 'posted' manually if it is complete)"
            )

        print(f"distribution {row.id} · hook {args.hook} · {len(tweets)} tweets")
        if resume_from:
            print(f"  resuming: {resume_from} already live, continuing from #{resume_from + 1}")
        for i, t in enumerate(tweets, 1):
            state = "  done   " if i <= resume_from else f"({len(t):>3}c) "
            flag = "  OVER-280!" if len(t) > 280 else ""
            print(f"  {i:>2}. {state}{flag} {t}")
        if args.dry_run:
            print("dry-run: nothing posted.")
            return 0

        prev_id: str | None = posted_ids[-1] if posted_ids else None
        first_id: str | None = posted_ids[0] if posted_ids else None
        try:
            for i in range(resume_from, len(tweets)):
                tid = _xurl_post_tweet(tweets[i], prev_id)
                first_id = first_id or tid
                prev_id = tid
                posted_ids.append(tid)
                print(f"  posted {i + 1}/{len(tweets)} -> {tid}")
                if i + 1 < len(tweets):
                    time.sleep(INTER_TWEET_DELAY)
        except Exception as exc:  # partial failure: record what is live, fail loudly
            url = _thread_url(first_id) if first_id else None
            row.status = "partial"  # recoverable: re-run --latest to resume
            row.external_url = url
            row.payload_json = {**payload, "posted_ids": posted_ids}
            session.commit()
            print(f"ERROR after partial post: {exc}", file=sys.stderr)
            print(
                f"  {len(posted_ids)}/{len(tweets)} tweets are live; "
                "re-run with --latest to resume from where it stopped",
                file=sys.stderr,
            )
            if url:
                print(f"partial thread root: {url}", file=sys.stderr)
            return 1

        url = _thread_url(first_id)  # first_id is set (loop ran at least once)
        row.status = "posted"
        row.variant = f"hook_{args.hook.lower()}"
        row.external_url = url
        row.payload_json = {**payload, "posted_ids": posted_ids}
        row.posted_at = datetime.now(timezone.utc)
        session.commit()
        print(f"thread live: {url}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
