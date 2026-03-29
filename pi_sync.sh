#!/usr/bin/env bash
# Sync Pi energy monitoring data to laptop.
# Run manually or via cron: */30 * * * * /path/to/pi_sync.sh
#
# Requires: SSH key auth to Pi (host "pi" in ~/.ssh/config)

set -euo pipefail

DEST="$HOME/.claude"
PI_HOST="${PI_HOST:-huginmunin.local}"

echo "Syncing Pi energy data from $PI_HOST..."

rsync -az "$PI_HOST:~/.claude/pi_journal.jsonl" "$DEST/pi_journal.jsonl" 2>/dev/null && \
    echo "  journal: OK" || echo "  journal: not found (Pi scanner may not have run yet)"

rsync -az "$PI_HOST:~/.claude/pi_daily_rollup.jsonl" "$DEST/pi_daily_rollup.jsonl" 2>/dev/null && \
    echo "  rollup:  OK" || echo "  rollup:  not found"

echo "Done."
