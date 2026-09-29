#!/usr/bin/env bash
# Pull energy monitoring data from remote machines to this machine.
# Run manually or via cron: */30 * * * * /path/to/remote_sync.sh
#
# Requires: SSH access to each remote host (see REMOTE_HOSTS below).
#
# Each remote machine produces, locally on itself:
#   ~/.claude/pi_journal.jsonl + pi_daily_rollup.jsonl                 (headless sessions, pi_scanner.py)
#   ~/.claude/interactive_journal_raw.jsonl + interactive_rollup_raw.jsonl  (interactive sessions, interactive_export.py)
# This script pulls all four down, one local copy per machine, named
# <tag>_journal.jsonl / <tag>_daily_rollup.jsonl / <tag>_interactive_journal.jsonl /
# <tag>_interactive_daily_rollup.jsonl so multiple machines don't overwrite each
# other. advisor.py/stepcount.py merge all of them by globbing *_journal.jsonl /
# *_daily_rollup.jsonl — no further code change needed to add a machine here.
# When energy-monitor is installed on a remote it also snapshots its history DB, which is
# copied to ~/.local/state/agentmon/remote/<tag>.sqlite3 and merged with `energy-monitor import`
# (see docs/remote-machines.md). Hosts without it print "history: not installed on <tag>".
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
    local tag="$1" host="$2" remote_name="$3" local_name="$4" label="$5"
    local rsync_err
    rsync_err=$(rsync -az "$host:~/.claude/$remote_name" "$DEST/$local_name" 2>&1) && {
        echo "  $label: OK"
        return
    }
    local rc=$?
    # rsync exits 23 ("some files/attrs were not transferred") for a missing
    # remote file, but also for other partial-transfer failures — permission
    # denied on the remote read, a local write failure, etc. — that exit code
    # alone doesn't distinguish them (confirmed: a faked rc=23 + "Permission
    # denied" would otherwise be misreported as a benign absence, same as a
    # real missing file). Require the actual "No such file or directory" text
    # rsync/openrsync both emit for a genuinely absent source file; anything
    # else at any exit code is a real error.
    if [[ $rc -eq 23 && "$rsync_err" == *"No such file or directory"* ]]; then
        echo "  $label: not found ($tag scanner may not have run yet)"
    else
        echo "  $label: ERROR (rsync exit $rc) — $rsync_err" >&2
    fi
}

# Merge the remote machine's history database (energy-monitor snapshot -> scp -> local import).
# Any failure is reported for this host only; the loop continues with the next one.
sync_history() {
    local tag="$1" host="$2"
    local state="$HOME/.local/state/agentmon" remote_dir
    remote_dir="$state/remote"
    if ! ssh "$host" 'command -v energy-monitor' >/dev/null 2>&1; then
        echo "  history: not installed on $tag"
        return 0
    fi
    if ! command -v energy-monitor >/dev/null 2>&1; then
        echo "  history: ERROR energy-monitor is not installed locally" >&2
        return 0
    fi
    mkdir -p "$remote_dir" && chmod 700 "$remote_dir" || { echo "  history: ERROR cannot create $remote_dir" >&2; return 0; }
    local err
    # Single quotes: the remote shell expands ~, not this one.
    if ! err=$(ssh "$host" energy-monitor snapshot '~/.local/state/agentmon/snapshot.sqlite3' 2>&1 >/dev/null); then
        echo "  history: ERROR snapshot failed on $tag: $err" >&2
        return 0
    fi
    if ! err=$(scp -q "$host:.local/state/agentmon/snapshot.sqlite3" "$remote_dir/$tag.sqlite3.part" 2>&1); then
        rm -f "$remote_dir/$tag.sqlite3.part"
        echo "  history: ERROR scp failed for $tag: $err" >&2
        return 0
    fi
    chmod 600 "$remote_dir/$tag.sqlite3.part"
    mv -f "$remote_dir/$tag.sqlite3.part" "$remote_dir/$tag.sqlite3"
    if err=$(energy-monitor import "$remote_dir/$tag.sqlite3" --label "$tag" 2>&1 >/dev/null); then
        echo "  history: OK"
    else
        echo "  history: ERROR import failed for $tag: $err" >&2
    fi
}

for entry in "${REMOTE_HOSTS[@]}"; do
    tag="${entry%%:*}"
    host="${entry#*:}"

    echo "Syncing energy data from $tag ($host)..."

    pull "$tag" "$host" "pi_journal.jsonl" "${tag}_journal.jsonl" "journal"
    pull "$tag" "$host" "pi_daily_rollup.jsonl" "${tag}_daily_rollup.jsonl" "rollup"
    pull "$tag" "$host" "interactive_journal_raw.jsonl" "${tag}_interactive_journal.jsonl" "interactive journal"
    pull "$tag" "$host" "interactive_rollup_raw.jsonl" "${tag}_interactive_daily_rollup.jsonl" "interactive rollup"
    sync_history "$tag" "$host"
done

echo "Done."
