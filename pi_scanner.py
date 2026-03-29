#!/usr/bin/env python3
"""Pi headless energy monitor — scans print-mode JSONL session files.

Parses Claude Code's JSONL transcript files from ~/.claude/projects/ on the
Pi (or any machine running headless `claude -p` tasks), extracts token usage,
and writes to an append-only invocation journal + derived daily rollup.

This is the decoupled v1 scanner. The target architecture is Hugin-side
capture, but this scanner avoids coupling to Hugin's evolving task format
and also captures non-Hugin Pi sessions.

Usage:
    python3 pi_scanner.py                    # global scan (cron catch-up)
    python3 pi_scanner.py --file <path>      # parse one specific JSONL file
    python3 pi_scanner.py --journal <path>   # custom journal location
    python3 pi_scanner.py --dry-run          # show what would be journaled
"""

import argparse
import fcntl
import hashlib
import json
import os
import socket
import sys
from collections import defaultdict
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
JOURNAL_FILE = CLAUDE_DIR / "pi_journal.jsonl"
ROLLUP_FILE = CLAUDE_DIR / "pi_daily_rollup.jsonl"

_SELF_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]

KNOWN_ENTRY_TYPES = {
    "user", "assistant", "progress", "system", "last-prompt",
    "file-history-snapshot", "queue-operation",
}


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def parse_jsonl(path):
    """Parse a JSONL file, returning metadata and deduplicated token usage.

    Returns None if the file has no completed assistant entries with usage.
    Otherwise returns a dict with session metadata and summed token fields.
    """
    session_id = None
    project = None
    first_ts = None
    model = None
    warnings = []

    # Collect assistant entries keyed by requestId (last wins for dedup)
    by_request = {}
    user_count = 0

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                if entry_type and entry_type not in KNOWN_ENTRY_TYPES:
                    warnings.append(f"unknown entry type: {entry_type}")

                # Extract session ID from any entry that has it
                if not session_id:
                    session_id = entry.get("sessionId")

                # Extract project from user entry's cwd
                if entry_type == "user":
                    user_count += 1
                    if not first_ts:
                        first_ts = entry.get("timestamp")
                    cwd = entry.get("cwd", "")
                    if cwd and not project:
                        project = os.path.basename(cwd)

                # Collect assistant entries with usage
                if entry_type == "assistant":
                    msg = entry.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        warnings.append("assistant entry without usage")
                        continue

                    request_id = entry.get("requestId", "")
                    if not request_id:
                        # Skip entries without requestId (rare, zero usage)
                        continue

                    # Extract model from message
                    entry_model = msg.get("model")
                    if entry_model:
                        model = entry_model

                    by_request[request_id] = usage

    except Exception as e:
        print(f"  Warning: could not read {path}: {e}", file=sys.stderr)
        return None

    if not session_id:
        if warnings:
            for w in warnings:
                print(f"  Warning [{path.name}]: {w}", file=sys.stderr)
        return None

    # Completion check: at least one assistant entry with nonzero usage
    if not by_request:
        return None

    # Sum token fields across deduplicated entries
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0

    for usage in by_request.values():
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cache_read += usage.get("cache_read_input_tokens", 0)
        total_cache_write += usage.get("cache_creation_input_tokens", 0)

    # Require at least some nonzero usage
    if total_input + total_output + total_cache_read + total_cache_write == 0:
        return None

    # Print non-critical warnings
    for w in warnings:
        print(f"  Warning [{path.name}]: {w}", file=sys.stderr)

    return {
        "sid": session_id,
        "ts": first_ts or "",
        "model": model or "?",
        "project": project or "?",
        "input": total_input,
        "output": total_output,
        "cache_read": total_cache_read,
        "cache_write": total_cache_write,
        "turns": len(by_request),
    }


# ---------------------------------------------------------------------------
# Journal operations
# ---------------------------------------------------------------------------

def load_journaled_sids(journal_path):
    """Load set of session IDs already in the journal."""
    sids = set()
    if not journal_path.exists():
        return sids
    try:
        with open(journal_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    sid = entry.get("sid")
                    if sid:
                        sids.add(sid)
                except json.JSONDecodeError:
                    # Skip malformed trailing lines (crash recovery)
                    continue
    except Exception:
        pass
    return sids


def append_journal(journal_path, entries):
    """Append entries to journal with flock for concurrency safety."""
    if not entries:
        return

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        data = "\n".join(json.dumps(e) for e in entries) + "\n"
        fd = os.open(str(journal_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, data.encode())
        os.close(fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def rebuild_rollup(journal_path, rollup_path):
    """Rewrite daily rollup from full journal (deterministic, repairable)."""
    by_date = defaultdict(lambda: {"input": 0, "output": 0,
                                    "cache_read": 0, "cache_write": 0,
                                    "sessions": 0})
    if not journal_path.exists():
        return

    with open(journal_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("ts", "")
            date_str = ts[:10] if len(ts) >= 10 else "unknown"
            d = by_date[date_str]
            d["input"] += entry.get("input", 0)
            d["output"] += entry.get("output", 0)
            d["cache_read"] += entry.get("cache_read", 0)
            d["cache_write"] += entry.get("cache_write", 0)
            d["sessions"] += 1

    # Atomic write: temp -> fsync -> rename
    tmp = rollup_path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        lines = []
        for date_str in sorted(by_date.keys()):
            d = by_date[date_str]
            lines.append(json.dumps({
                "date": date_str,
                "input": d["input"],
                "output": d["output"],
                "cache_read": d["cache_read"],
                "cache_write": d["cache_write"],
                "sessions": d["sessions"],
                "machine": _machine_id(),
                "v": _SELF_HASH,
            }))
        os.write(fd, ("\n".join(lines) + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        os.rename(str(tmp), str(rollup_path))
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _machine_id():
    """Get machine identifier."""
    # Check for explicit config first
    id_file = CLAUDE_DIR / "pi_machine_id"
    if id_file.exists():
        try:
            return id_file.read_text().strip()
        except Exception:
            pass
    return socket.gethostname()


# ---------------------------------------------------------------------------
# Scan modes
# ---------------------------------------------------------------------------

def find_jsonl_files(projects_dir):
    """Find all JSONL session files recursively."""
    files = []
    if not projects_dir.exists():
        return files
    for jsonl in projects_dir.rglob("*.jsonl"):
        if jsonl.name.startswith("."):
            continue
        files.append(jsonl)
    return files


def scan_files(files, journal_path, dry_run=False):
    """Scan JSONL files and journal new entries.

    Returns list of new journal entries.
    """
    # Load known session IDs (outside lock — read-only)
    known_sids = load_journaled_sids(journal_path)

    new_entries = []
    skipped_known = 0
    skipped_empty = 0

    for path in files:
        result = parse_jsonl(path)
        if result is None:
            skipped_empty += 1
            continue

        sid = result["sid"]
        if sid in known_sids:
            skipped_known += 1
            continue

        entry = {
            "ts": result["ts"],
            "sid": sid,
            "machine": _machine_id(),
            "model": result["model"],
            "project": result["project"],
            "input": result["input"],
            "output": result["output"],
            "cache_read": result["cache_read"],
            "cache_write": result["cache_write"],
            "turns": result["turns"],
            "v": _SELF_HASH,
        }
        new_entries.append(entry)
        known_sids.add(sid)  # prevent dupes within this run

    if not dry_run and new_entries:
        append_journal(journal_path, new_entries)

    return new_entries, skipped_known, skipped_empty


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pi headless energy monitor — scan JSONL session files")
    parser.add_argument("--file", type=str,
                        help="parse one specific JSONL file (hook mode)")
    parser.add_argument("--journal", type=str, default=str(JOURNAL_FILE),
                        help=f"journal file path (default: {JOURNAL_FILE})")
    parser.add_argument("--rollup", type=str, default=str(ROLLUP_FILE),
                        help=f"rollup file path (default: {ROLLUP_FILE})")
    parser.add_argument("--projects-dir", type=str,
                        default=str(PROJECTS_DIR),
                        help=f"projects directory (default: {PROJECTS_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be journaled without writing")
    args = parser.parse_args()

    journal_path = Path(args.journal)
    rollup_path = Path(args.rollup)

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        files = [path]
    else:
        projects_dir = Path(args.projects_dir)
        files = find_jsonl_files(projects_dir)
        if not files:
            print("No JSONL files found.", file=sys.stderr)
            sys.exit(0)
        print(f"Scanning {len(files)} JSONL files...", file=sys.stderr)

    new_entries, skipped_known, skipped_empty = scan_files(
        files, journal_path, dry_run=args.dry_run)

    if args.dry_run:
        for entry in new_entries:
            print(json.dumps(entry, indent=2))
        print(f"\n{len(new_entries)} new, {skipped_known} known, "
              f"{skipped_empty} empty/incomplete", file=sys.stderr)
        return

    if new_entries:
        print(f"Journaled {len(new_entries)} new session(s), "
              f"{skipped_known} known, {skipped_empty} empty",
              file=sys.stderr)
    else:
        print(f"No new sessions ({skipped_known} known, "
              f"{skipped_empty} empty)", file=sys.stderr)

    # Rebuild rollup from journal
    rebuild_rollup(journal_path, rollup_path)


if __name__ == "__main__":
    main()
