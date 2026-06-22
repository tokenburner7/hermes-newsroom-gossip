#!/usr/bin/env bash
# Full newsroom cycle: ingest all -> run+publish one article -> (only if a NEW
# article was published) repackage into X/Telegram payloads -> rebuild + redeploy.
#
# EDGE CASE (handled): if no new article is published this cycle — arXiv had no
# papers matching the filter, or no candidate passes select/gate — distribute and
# deploy are SKIPPED. The site content is unchanged, so there is nothing to ship.
# Posting to X/Telegram is a separate agent step (post_thread.py / telegram skill).
set -euo pipefail

REPO="/Users/tn/dev/hermes-newsroom"
UV="/Users/tn/.local/bin/uv"             # from `which uv`
PROJECT="aixcrypto-news"                  # Cloudflare Pages project
LOG_DIR="$REPO/scripts/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%dT%H%M%S)"
exec >>"$LOG_DIR/cycle-$STAMP.log" 2>&1

cd "$REPO"
echo "=== pipeline_cycle $STAMP ==="

# Print the latest published article id (empty string if none). stderr is kept (it
# lands in the cycle log via the `exec >> log` above) so a DB/import failure is
# visible instead of silently swallowed.
latest_published_id() {
  "$UV" run python -c \
    "from newsroom.distribute import latest_published_article_id as f; print(f() or '')" \
    | tail -1
}

# `|| true` so a failing helper (DB down, import error) under `set -euo pipefail`
# does not silently abort the whole cycle on the command-substitution assignment.
BEFORE_ID="$(latest_published_id || true)"
echo "latest published before: '${BEFORE_ID:-<none>}'"

# 1. ingest every source (arXiv + the 8 others), then attempt one run + publish.
#    Non-zero from these is tolerated — the id diff below is the source of truth.
"$UV" run newsroom ingest-all || echo "WARN: ingest-all returned non-zero (continuing)"
"$UV" run newsroom run-once --publish || echo "WARN: run-once returned non-zero (no publish?)"

AFTER_ID="$(latest_published_id || true)"
echo "latest published after:  '${AFTER_ID:-<none>}'"

# 2. No-new-article cycle -> no-op. Nothing changed; do not distribute or redeploy.
if [ -z "$AFTER_ID" ] || [ "$AFTER_ID" = "$BEFORE_ID" ]; then
  echo "no new article published this cycle — skipping distribute + deploy."
  echo "=== cycle complete (no-op) $STAMP ==="
  exit 0
fi
echo "new article published: id=$AFTER_ID"

# 3. repackage the just-published article into X + Telegram payloads (idempotent).
"$UV" run newsroom distribute "$AFTER_ID" --channel all \
  || echo "WARN: distribute failed (continuing to deploy the new article)"

# 4. rebuild the static site (picks up the new Markdown) and redeploy to CF Pages.
cd "$REPO/web"
npm run build
npx --yes wrangler pages deploy dist --project-name "$PROJECT" --branch main

echo "=== cycle complete $STAMP ==="
