#!/bin/bash
# Wrapper for the gossip newsroom cycle — cron runs this, not cycle.py directly.
# Ensures uv-managed Python is used regardless of cron's PATH.
set -e
cd /Users/tn/dev/hermes-newsroom-v2-gossip
# Cron inherits a stale VIRTUAL_ENV from the launching shell, which makes uv warn
# "VIRTUAL_ENV does not match the project environment path .venv" every cycle.
# Unset it so uv resolves the project's own .venv cleanly (F5).
unset VIRTUAL_ENV
exec /Users/tn/.local/bin/uv run python scripts/cycle.py
