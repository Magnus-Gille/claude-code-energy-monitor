#!/usr/bin/env bash
# Reference TokenAtlas collector for cron/launchd. Install by copying it next to remote_sync.sh, e.g.
#   cp scripts/collect.sh remote_sync.sh ~/.local/share/tokenatlas/ && chmod +x ~/.local/share/tokenatlas/*.sh
#
# Order (local results never wait for remotes):
#   1. tokenatlas refresh --all
#   2. tokenatlas top --keep-text            only if top-prompts.json exists in the state dir (opt-in)
#   3. tokenatlas report --html ... --private --if-changed --max-age 1h
#   4. remote_sync.sh                         only if REMOTE_HOSTS_OVERRIDE is set or <state>/remote-hosts exists
#                                             (a file of space-separated tag:host pairs); bounded by
#                                             TOKENATLAS_HOST_TIMEOUT / TOKENATLAS_SSH_OPTS, see remote_sync.sh
#   5. the conditional report again if the remote sync succeeded (new data shows up without waiting an hour)
#
# One run at a time: a mkdir lock under the state dir holds a PID file. A lock whose PID is dead, or that is
# older than 2 h, is taken over (logged). Log: one line per step with its exit code, on stdout.
#
# Environment: TOKENATLAS_STATE_DIR (default $XDG_STATE_HOME/tokenatlas or ~/.local/state/tokenatlas),
# TOKENATLAS_BIN (default tokenatlas), TOKENATLAS_REMOTE_SYNC (default remote_sync.sh beside this script),
# TOKENATLAS_LOCK_MAX_AGE_MIN (default 120).

set -u
export PATH="$HOME/.local/bin:$PATH"

STATE="${TOKENATLAS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tokenatlas}"
BIN="${TOKENATLAS_BIN:-tokenatlas}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_SYNC="${TOKENATLAS_REMOTE_SYNC:-$HERE/remote_sync.sh}"
LOCK="$STATE/collect.lock"
MAX_AGE_MIN="${TOKENATLAS_LOCK_MAX_AGE_MIN:-120}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') collect: $*"; }

mkdir -p "$STATE" || { log "cannot create $STATE"; exit 1; }

acquire_lock() {
    local pid attempt
    for attempt in 1 2; do
        if mkdir "$LOCK" 2>/dev/null; then
            echo $$ > "$LOCK/pid"
            return 0
        fi
        pid=$(cat "$LOCK/pid" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null &&
           [[ -z "$(find "$LOCK" -maxdepth 0 -mmin "+$MAX_AGE_MIN" 2>/dev/null)" ]]; then
            return 1
        fi
        log "taking over stale lock (pid ${pid:-unknown})"
        rm -rf -- "$LOCK"
    done
    return 1
}

if ! acquire_lock; then
    log "already running (lock $LOCK, pid $(cat "$LOCK/pid" 2>/dev/null || echo unknown)); skipping"
    exit 0
fi
trap 'rm -rf -- "$LOCK"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT HUP

rc_all=0
step() {
    local name="$1" rc=0
    shift
    "$@" >/dev/null || rc=$?
    log "$name exit=$rc"
    [[ $rc -eq 0 ]] || rc_all=1
    return "$rc"
}

report() {
    step "$1" "$BIN" report --html "$STATE/report.html" --private --if-changed "${@:2}"
}

step refresh "$BIN" refresh --all || true
if [[ -f "$STATE/top-prompts.json" ]]; then
    step top "$BIN" top --keep-text || true
fi
report report --max-age 1h || true

if [[ -z "${REMOTE_HOSTS_OVERRIDE:-}" && -s "$STATE/remote-hosts" ]]; then
    REMOTE_HOSTS_OVERRIDE="$(tr '\n' ' ' < "$STATE/remote-hosts")"
fi
if [[ -n "${REMOTE_HOSTS_OVERRIDE:-}" ]]; then
    export REMOTE_HOSTS_OVERRIDE
    if [[ -f "$REMOTE_SYNC" ]]; then
        if step remote-sync bash "$REMOTE_SYNC"; then
            report report-after-sync || true
        fi
    else
        log "remote-sync skipped: $REMOTE_SYNC not found"
    fi
fi
exit "$rc_all"
