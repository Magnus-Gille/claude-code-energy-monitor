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
# When tokenatlas (or the deprecated energy-monitor) is installed on a remote it also snapshots its history DB, which is
# copied to ~/.local/state/tokenatlas/remote/<tag>.sqlite3 and merged with `tokenatlas import`
# (see docs/remote-machines.md). Hosts without it print "history: not installed on <tag>".
# A remote with no headless scanner (or no interactive use) just reports "not found"
# for the files it doesn't produce.
#
# This same script runs on multiple machines with different REMOTE_HOSTS, so the
# data flows as a mesh rather than only into one hub. Override the default host
# list via REMOTE_HOSTS_OVERRIDE (space-separated tag:host pairs), e.g. on m5's
# cron pulling only from the laptop:
#   REMOTE_HOSTS_OVERRIDE="laptop:magnus-macbook-air" /path/to/remote_sync.sh
#
# TOKENATLAS_DB (optional; ENERGY_MONITOR_DB is still honoured as a fallback): if set and non-empty,
# the local import goes into that database (`tokenatlas --db "$TOKENATLAS_DB" import ...`) instead of the default one, e.g. to try
# the sync without touching your real history.

# Bounded remote calls (a stalled host must never hang the run):
#   TOKENATLAS_SSH_OPTS     options for every ssh/scp/rsync-ssh call (default below)
#   TOKENATLAS_HOST_TIMEOUT overall seconds per host (default 300); a host over the limit is killed (with its
#                           children) and reported as "<host>: ERROR (timeout after Ns)", then the next host runs.
#                           The script then exits 1 once it has finished the remaining hosts.

set -euo pipefail

# Cron has a minimal PATH, and pipx/venv installs link tokenatlas into ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

DEST="$HOME/.claude"

SSH_OPTS_STR="${TOKENATLAS_SSH_OPTS:--o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o BatchMode=yes}"
read -ra SSH_OPTS <<< "$SSH_OPTS_STR"
HOST_TIMEOUT="${TOKENATLAS_HOST_TIMEOUT:-300}"
if ! [[ "$HOST_TIMEOUT" =~ ^[0-9]+$ ]] || [[ "$HOST_TIMEOUT" -eq 0 ]]; then
    echo "TOKENATLAS_HOST_TIMEOUT must be a positive integer (seconds), got '$HOST_TIMEOUT'" >&2
    exit 2
fi

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

# Tags become file names and hosts become ssh/scp/rsync arguments: accept only plain characters and never a
# host that could be read as an option.  (rsync gets `--` too; ssh/scp get it before the host.)
valid_pair() {
    [[ "$1" =~ ^[A-Za-z0-9_-]+$ && "$2" =~ ^[A-Za-z0-9._@:-]+$ && "$2" != -* ]]
}

pull() {
    local tag="$1" host="$2" remote_name="$3" local_name="$4" label="$5"
    local rsync_err
    rsync_err=$(rsync -az --timeout=60 -e "ssh $SSH_OPTS_STR" -- "$host:~/.claude/$remote_name" "$DEST/$local_name" 2>&1) && {
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

# Merge the remote machine's history database (tokenatlas snapshot -> scp -> local import).
# Any failure is reported for this host only; the loop continues with the next one.
sync_history() {
    local tag="$1" host="$2"
    local state="$HOME/.local/state/tokenatlas" remote_dir
    remote_dir="$state/remote"
    # Non-interactive ssh has a minimal PATH too, so each remote call prepends ~/.local/bin.
    # Single quotes: the remote shell expands $HOME and ~, not this one.
    if ! ssh "${SSH_OPTS[@]}" -- "$host" 'PATH="$HOME/.local/bin:$PATH"; command -v tokenatlas || command -v energy-monitor' >/dev/null 2>&1; then
        echo "  history: not installed on $tag"
        return 0
    fi
    if ! command -v tokenatlas >/dev/null 2>&1; then
        echo "  history: ERROR tokenatlas is not installed locally" >&2
        return 0
    fi
    { mkdir -p "$remote_dir" && chmod 700 "$remote_dir"; } || { echo "  history: ERROR cannot create $remote_dir" >&2; return 0; }
    local err
    if ! err=$(ssh "${SSH_OPTS[@]}" -- "$host" 'PATH="$HOME/.local/bin:$PATH"; c=$(command -v tokenatlas || command -v energy-monitor) && "$c" snapshot ~/.local/state/tokenatlas/snapshot.sqlite3' 2>&1 >/dev/null); then
        echo "  history: ERROR snapshot failed on $tag: $err" >&2
        return 0
    fi
    if ! err=$(scp -q "${SSH_OPTS[@]}" -- "$host:.local/state/tokenatlas/snapshot.sqlite3" "$remote_dir/$tag.sqlite3.part" 2>&1); then
        rm -f -- "$remote_dir/$tag.sqlite3.part" 2>/dev/null || true
        echo "  history: ERROR scp failed for $tag: $err" >&2
        return 0
    fi
    # Under set -e an unguarded failure here would abort the whole loop, not just this host.
    if ! err=$(chmod 600 "$remote_dir/$tag.sqlite3.part" 2>&1 &&
               mv -f "$remote_dir/$tag.sqlite3.part" "$remote_dir/$tag.sqlite3" 2>&1); then
        rm -f -- "$remote_dir/$tag.sqlite3.part" 2>/dev/null || true
        echo "  history: ERROR cannot store snapshot for $tag: $err" >&2
        return 0
    fi
    local db_args=()
    local db="${TOKENATLAS_DB:-${ENERGY_MONITOR_DB:-}}"
    [[ -n "$db" ]] && db_args=(--db "$db")
    if err=$(tokenatlas "${db_args[@]+"${db_args[@]}"}" import "$remote_dir/$tag.sqlite3" --label "$tag" 2>&1 >/dev/null); then
        echo "  history: OK"
    else
        echo "  history: ERROR import failed for $tag: $err" >&2
    fi
}

# Kill a process and everything below it. Under `set -m` a background job is its own process-group leader
# (pgid == pid) on both macOS bash 3.2 and Linux bash 5, so one group kill reaches the whole tree; the
# pgrep -P walk covers children that moved to another group or a shell where job control is unavailable.
kill_tree() {
    local pid="$1" sig="$2" child
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        kill_tree "$child" "$sig"
    done
    kill "-$sig" -- "-$pid" 2>/dev/null || true
    kill "-$sig" "$pid" 2>/dev/null || true
}

sync_host() {
    local tag="$1" host="$2"
    echo "Syncing energy data from $tag ($host)..."
    pull "$tag" "$host" "pi_journal.jsonl" "${tag}_journal.jsonl" "journal"
    pull "$tag" "$host" "pi_daily_rollup.jsonl" "${tag}_daily_rollup.jsonl" "rollup"
    pull "$tag" "$host" "interactive_journal_raw.jsonl" "${tag}_interactive_journal.jsonl" "interactive journal"
    pull "$tag" "$host" "interactive_rollup_raw.jsonl" "${tag}_interactive_daily_rollup.jsonl" "interactive rollup"
    sync_history "$tag" "$host"
}

# Run sync_host in a background subshell with a deadline. Returns 124 on timeout.
run_with_deadline() {
    local pid deadline waited=0
    set -m
    ( sync_host "$1" "$2" ) &
    pid=$!
    set +m
    deadline=$HOST_TIMEOUT
    while kill -0 "$pid" 2>/dev/null; do
        if [[ $waited -ge $deadline ]]; then
            kill_tree "$pid" TERM
            sleep 1
            kill_tree "$pid" KILL
            { wait "$pid"; } 2>/dev/null || true
            return 124
        fi
        sleep 1
        waited=$((waited + 1))
    done
    local rc=0
    wait "$pid" || rc=$?
    return "$rc"
}

overall_rc=0
for entry in "${REMOTE_HOSTS[@]}"; do
    tag="${entry%%:*}"
    host="${entry#*:}"
    if [[ "$entry" != *:* ]] || ! valid_pair "$tag" "$host"; then
        echo "Skipping invalid tag:host entry '$entry'" >&2
        continue
    fi

    rc=0
    run_with_deadline "$tag" "$host" || rc=$?
    if [[ $rc -eq 124 ]]; then
        echo "$host: ERROR (timeout after ${HOST_TIMEOUT}s)" >&2
        overall_rc=1
    elif [[ $rc -ne 0 ]]; then
        echo "$host: ERROR (host sync exit $rc)" >&2
        overall_rc=1
    fi
done

echo "Done."
exit "$overall_rc"
