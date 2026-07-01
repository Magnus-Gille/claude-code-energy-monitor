#!/usr/bin/env bash
# Sync headless energy monitoring data from remote machines to this laptop.
# Run manually or via cron: */30 * * * * /path/to/pi_sync.sh
#
# Requires: SSH key auth to each remote host (see REMOTE_HOSTS below).
#
# Each remote machine runs pi_scanner.py locally, producing
# ~/.claude/pi_journal.jsonl + ~/.claude/pi_daily_rollup.jsonl on itself.
# This script pulls those files down, one local copy per machine, named
# <tag>_journal.jsonl / <tag>_daily_rollup.jsonl so multiple machines don't
# overwrite each other. advisor.py/stepcount.py merge all of them by globbing
# *_journal.jsonl / *_daily_rollup.jsonl — no further code change needed to
# add a machine here.

set -euo pipefail

DEST="$HOME/.claude"

# tag:host pairs. Override a host via env var, e.g. PI_HOST=otherpi.local
REMOTE_HOSTS=(
    "pi:${PI_HOST:-huginmunin.local}"
    "m5:${M5_HOST:-m5}"
)

for entry in "${REMOTE_HOSTS[@]}"; do
    tag="${entry%%:*}"
    host="${entry#*:}"

    echo "Syncing energy data from $tag ($host)..."

    rsync -az "$host:~/.claude/pi_journal.jsonl" "$DEST/${tag}_journal.jsonl" 2>/dev/null && \
        echo "  journal: OK" || echo "  journal: not found ($tag scanner may not have run yet)"

    rsync -az "$host:~/.claude/pi_daily_rollup.jsonl" "$DEST/${tag}_daily_rollup.jsonl" 2>/dev/null && \
        echo "  rollup:  OK" || echo "  rollup:  not found"
done

echo "Done."
