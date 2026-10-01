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
# One run at a time: a mkdir lock under the state dir holds an owner file (pid, start time, nonce). A lock whose
# pid is dead, or alive with a different start time (pid reused), is taken over (logged). A verified live owner is
# never taken over; past TOKENATLAS_LOCK_MAX_AGE_MIN the run only logs a warning and exits. Log: one line per step with its exit code, on stdout.
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

# Lock layout: $LOCK/owner holds three lines (pid, process start time, random nonce), published atomically
# (write owner.$$, then mv). A lock dir without an owner younger than 60 s is still being created: held.
NONCE="$RANDOM$RANDOM$RANDOM-$(date +%s)-$$"
LOCK_GRACE_SEC=60

proc_start() { ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//; s/ *$//'; }

file_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

publish_owner() {
    local start
    start=$(proc_start $$)
    { printf '%s\n%s\n%s\n' "$$" "$start" "$NONCE" > "$LOCK/owner.$$" &&
      mv -f "$LOCK/owner.$$" "$LOCK/owner"; } 2>/dev/null
}

# Remove our lock only if it is still ours (the owner file still carries our nonce).
release_lock() {
    local pid start nonce
    if [[ -f "$LOCK/owner" ]]; then
        { read -r pid; read -r start; read -r nonce; } < "$LOCK/owner" 2>/dev/null || true
        [[ "${nonce:-}" == "$NONCE" ]] && rm -rf -- "$LOCK"
    fi
    return 0
}

# Return 0 with the lock held, 1 if another run holds it (or acquisition failed).
acquire_lock() {
    local attempt pid start nonce cur now mtime age_min
    for attempt in 1 2 3 4 5; do
        if mkdir "$LOCK" 2>/dev/null; then
            if ! publish_owner; then
                rm -rf -- "$LOCK"
                log "cannot write lock owner in $LOCK"
                return 1
            fi
            return 0
        fi
        pid="" start="" nonce=""
        if [[ -f "$LOCK/owner" ]]; then
            { read -r pid; read -r start; read -r nonce; } < "$LOCK/owner" 2>/dev/null || true
            if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
                cur=$(proc_start "$pid")
                if [[ -n "$cur" && "$cur" == "$start" ]]; then
                    # Verified live owner: never taken over, however old.
                    now=$(date +%s)
                    mtime=$(file_mtime "$LOCK/owner" || echo "$now")
                    age_min=$(( (now - mtime) / 60 ))
                    if [[ $age_min -gt $MAX_AGE_MIN ]]; then
                        log "warning: lock held by live pid $pid for $age_min min"
                    fi
                    return 1
                fi
            fi
        else
            if [[ ! -d "$LOCK" ]]; then
                continue  # released between mkdir and now; retry
            fi
            now=$(date +%s)
            mtime=$(file_mtime "$LOCK" || echo "$now")
            if [[ $((now - mtime)) -lt $LOCK_GRACE_SEC ]]; then
                return 1  # being created by another run
            fi
        fi
        # Stale. mv is atomic, so exactly one contender wins; the others re-read.
        if mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
            local moved=""
            if [[ -f "$LOCK.stale.$$/owner" ]]; then
                { read -r _ ; read -r _ ; read -r moved; } < "$LOCK.stale.$$/owner" 2>/dev/null || true
            fi
            if [[ "$moved" != "$nonce" ]]; then
                # Someone replaced the lock between our read and the mv: put it back if the slot is free.
                [[ -e "$LOCK" ]] || mv "$LOCK.stale.$$" "$LOCK" 2>/dev/null || true
                [[ -e "$LOCK.stale.$$" ]] && rm -rf -- "$LOCK.stale.$$"
                continue
            fi
            log "taking over stale lock (pid ${pid:-unknown})"
            rm -rf -- "$LOCK.stale.$$"
        fi
    done
    return 1
}

if ! acquire_lock; then
    holder=$(sed -n 1p "$LOCK/owner" 2>/dev/null || true)
    log "already running (lock $LOCK, pid ${holder:-unknown}); skipping"
    exit 0
fi
trap release_lock EXIT
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
