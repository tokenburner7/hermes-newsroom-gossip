#!/usr/bin/env bash
# Fast arXiv-only refresh (the speed wedge). Runs every 3 hours.
# Ingesting zero new papers is a normal, graceful no-op: it logs and exits 0.
# This leg never publishes — it only freshens the candidate pool for the cycle.
# On weekends (Sat/Sun) arXiv publishes nothing — widen the lookback to 2d so
# Friday's papers are still captured instead of returning 0 every cycle (F2+F3).
set -euo pipefail
REPO="/Users/tn/dev/hermes-newsroom"
UV="/Users/tn/.local/bin/uv"
LOG_DIR="$REPO/scripts/logs"
mkdir -p "$LOG_DIR"
cd "$REPO"
echo "=== arxiv_ingest $(date +%Y%m%dT%H%M%S) ===" >>"$LOG_DIR/arxiv.log"
SINCE="3h"
if [ "$(date -u +%u)" -ge 6 ]; then SINCE="2d"; fi
"$UV" run newsroom ingest --since "$SINCE" >>"$LOG_DIR/arxiv.log" 2>&1
