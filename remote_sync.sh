#!/usr/bin/env bash
# Pull energy monitoring data from remote machines to this machine.
# Run manually or via cron: */30 * * * * /path/to/remote_sync.sh
#
# Requires: SSH access to each remote host (see REMOTE_HOSTS below).
#
# Each remote machine produces, locally on itself:
#   ~/.claude/pi_journal.jsonl + pi_daily_rollup.jsonl       (headless sessions, pi_scanner.py)
#   ~/.claude/interactive_journal.jsonl + interactive_daily_rollup.jsonl  (interactive sessions, interactive_export.py)
# This script pulls all four down, one local copy per machine, named
# <tag>_journal.jsonl / <tag>_daily_rollup.jsonl / <tag>_interactive_journal.jsonl /
# <tag>_interactive_daily_rollup.jsonl so multiple machines don't overwrite each
# other. advisor.py/stepcount.py merge all of them by globbing *_journal.jsonl /
# *_daily_rollup.jsonl — no further code change needed to add a machine here.
# A remote with no headless scanner (or no interactive use) just reports "not found"
# for the files it doesn't produce.
#
# This same script runs on multiple machines with different REMOTE_HOSTS, so the
# data flows as a mesh rather than only into one hub. Override the default host
# list via REMOTE_HOSTS_OVERRIDE (space-separated tag:host pairs), e.g. on m5's
# cron pulling only from the laptop:
#   REMOTE_HOSTS_OVERRIDE="laptop:magnus-macbook-air" /path/to/remote_sync.sh

set -euo pipefail

DEST="$HOME/.claude"

# tag:host pairs. Override a host via env var, e.g. PI_HOST=otherpi.local
DEFAULT_REMOTE_HOSTS=(
    "pi:${PI_HOST:-huginmunin.local}"
    "m5:${M5_HOST:-m5}"
)

if [[ -n "${REMOTE_HOSTS_OVERRIDE:-}" ]]; then
    read -ra REMOTE_HOSTS <<< "$REMOTE_HOSTS_OVERRIDE"
else
    REMOTE_HOSTS=("${DEFAULT_REMOTE_HOSTS[@]}")
fi

pull() {
    local host="$1" remote_name="$2" local_name="$3" label="$4"
    rsync -az "$host:~/.claude/$remote_name" "$DEST/$local_name" 2>/dev/null && \
        echo "  $label: OK" || echo "  $label: not found"
}

for entry in "${REMOTE_HOSTS[@]}"; do
    tag="${entry%%:*}"
    host="${entry#*:}"

    echo "Syncing energy data from $tag ($host)..."

    pull "$host" "pi_journal.jsonl" "${tag}_journal.jsonl" "journal"
    pull "$host" "pi_daily_rollup.jsonl" "${tag}_daily_rollup.jsonl" "rollup"
    pull "$host" "interactive_journal.jsonl" "${tag}_interactive_journal.jsonl" "interactive journal"
    pull "$host" "interactive_daily_rollup.jsonl" "${tag}_interactive_daily_rollup.jsonl" "interactive rollup"
done

echo "Done."
