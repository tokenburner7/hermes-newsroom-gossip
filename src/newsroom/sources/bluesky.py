"""Bluesky (AT Protocol) ingestion source — credential-gated.

If both ``BLUESKY_HANDLE`` and ``BLUESKY_APP_PASSWORD`` are configured this logs
in with an app password and runs a ``searchPosts`` query, upserting matching
posts. Without credentials it returns ``(0, 0)`` so ``ingest-all`` stays green.

Wiring it up later
------------------
1. Create an *app password* at https://bsky.app/settings/app-passwords (never
   use your account password) and set in ``.env``::

       BLUESKY_HANDLE=you.bsky.social
       BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx

2. Auth flow used below (public ``bsky.social`` PDS / AppView):
     POST com.atproto.server.createSession  {identifier, password} -> accessJwt
     GET  app.bsky.feed.searchPosts        ?q=...&limit=N  (Bearer accessJwt)

3. To go further: refresh the session with ``com.atproto.server.refreshSession``
   (the access JWT is short-lived), paginate via the ``cursor`` field, or swap
   the raw HTTP calls for the official ``atproto`` Python SDK. Search relevance
   and rate limits are governed by the AppView; keep queries focused.

  external_id = post AT-URI (at://did/app.bsky.feed.post/rkey)
  cleaned_text = "@{handle}: {post text}"
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ._base import SourceItem, upsert_items

SOURCE_CLASS = "bluesky"

log = logging.getLogger(__name__)

PDS_BASE = "https://bsky.social/xrpc"
APPVIEW_BASE = "https://public.api.bsky.app/xrpc"
SEARCH_QUERY = "AI crypto regulation"
SEARCH_LIMIT = 25

RATE_LIMIT_S = 1.0
HTTP_TIMEOUT_S = 30.0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _post_url(handle: str, uri: str) -> str:
    """Map an at:// post URI to its public bsky.app web URL."""
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _normalize(posts: list[dict]) -> list[SourceItem]:
    items: list[SourceItem] = []
    for post in posts:
        uri = post.get("uri")
        record = post.get("record") or {}
        text = (record.get("text") or "").strip()
        author = post.get("author") or {}
        handle = author.get("handle") or "unknown"
        if not uri or not text:
            continue
        items.append(
            SourceItem(
                external_id=uri,
                url=_post_url(handle, uri),
                title=f"@{handle}",
                cleaned_text=f"@{handle}: {text}",
                published_at=_parse_dt(record.get("createdAt"))
                or _parse_dt(post.get("indexedAt")),
                categories=[SOURCE_CLASS],
            )
        )
    return items


async def _create_session(client: httpx.AsyncClient) -> str | None:
    """Log in with the app password; return the access JWT or None on failure."""
    try:
        resp = await client.post(
            f"{PDS_BASE}/com.atproto.server.createSession",
            json={
                "identifier": settings.bluesky_handle,
                "password": settings.bluesky_app_password,
            },
        )
        resp.raise_for_status()
        return resp.json().get("accessJwt")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Bluesky login failed: %s", exc)
        return None


async def fetch() -> list[SourceItem]:
    """Log in and run the search query. No-op (empty) without credentials."""
    if not (settings.bluesky_handle and settings.bluesky_app_password):
        log.info("Bluesky credentials not set; skipping bluesky source.")
        return []

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        token = await _create_session(client)
        if not token:
            return []
        await asyncio.sleep(RATE_LIMIT_S)
        try:
            resp = await client.get(
                f"{APPVIEW_BASE}/app.bsky.feed.searchPosts",
                params={"q": SEARCH_QUERY, "limit": SEARCH_LIMIT},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Bluesky search failed: %s", exc)
            return []
    return _normalize(payload.get("posts") or [])


async def ingest() -> tuple[int, int]:
    """Search Bluesky and upsert posts. Returns ``(fetched, upserted)``.

    Returns ``(0, 0)`` cleanly when credentials are unset (the default).
    """
    items = await fetch()
    upserted = await upsert_items(SOURCE_CLASS, items)
    return len(items), upserted
