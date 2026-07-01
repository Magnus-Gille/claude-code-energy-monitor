#!/usr/bin/env python3
"""Interactive-session journal exporter.

Converts this machine's local interactive Claude Code usage — statusline.py's
statusline_session_history.jsonl (archived days) plus statusline_daily.json
(today, in progress) — into the same journal/daily-rollup schema pi_scanner.py
produces for headless sessions. remote_sync.sh moves the result to other
machines exactly like the headless journal, so advisor.py/stepcount.py merge
it via the existing *_journal.jsonl / *_daily_rollup.jsonl glob — no separate
merge path needed.

Regenerates both output files from scratch each run; the statusline files are
the source of truth and this script holds no state of its own, so re-running
it is always safe (no double-counting).

Usage:
    python3 interactive_export.py
"""

import json
import os
import socket
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSION_HISTORY_FILE = CLAUDE_DIR / "statusline_session_history.jsonl"
DAILY_FILE = CLAUDE_DIR / "statusline_daily.json"
# Deliberately do NOT end these filenames in "_journal.jsonl" / "_daily_rollup.jsonl":
# advisor.py/stepcount.py glob for exactly that suffix to merge in *remote* machines'
# data, and this machine's own interactive usage is already counted via the native
# statusline_* files above — a name that matched the glob got this machine's own
# data double-counted locally (caught in review). remote_sync.sh reads these two
# raw filenames directly (not by glob) and writes the synced copy under the
# glob-matching <tag>_interactive_journal.jsonl name on the *other* machine.
JOURNAL_FILE = CLAUDE_DIR / "interactive_journal_raw.jsonl"
ROLLUP_FILE = CLAUDE_DIR / "interactive_rollup_raw.jsonl"


def _machine_id():
    """Same convention as pi_scanner.py, so entries from the same machine
    carry the same machine tag regardless of which scanner wrote them."""
    id_file = CLAUDE_DIR / "pi_machine_id"
    if id_file.exists():
        try:
            return id_file.read_text().strip()
        except Exception:
            pass
    return socket.gethostname()


def _load_jsonl(path):
    records = []
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _atomic_write_lines(path, lines):
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode() if lines else b"")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(str(tmp), str(path))


def main():
    machine = _machine_id()
    by_date = {}
    journal_lines = []

    def add_session_day(date_str, sid, model, project, n, di, do, dc, dcw):
        journal_lines.append(json.dumps({
            "ts": f"{date_str}T00:00:00Z",
            "sid": sid,
            "machine": machine,
            "model": model,
            "project": project,
            "input": di,
            "output": do,
            "cache_read": dc,
            "cache_write": dcw,
            "turns": n,
            "v": "interactive",
        }))
        d = by_date.setdefault(date_str, {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "sessions": 0,
        })
        d["input"] += di
        d["output"] += do
        d["cache_read"] += dc
        d["cache_write"] += dcw
        d["sessions"] += 1

    for rec in _load_jsonl(SESSION_HISTORY_FILE):
        add_session_day(
            rec.get("date", "?"), rec.get("sid", "?"),
            rec.get("m", "?"), rec.get("p", "?"), rec.get("n", 0),
            rec.get("di", 0), rec.get("do", 0), rec.get("dc", 0), rec.get("dcw", 0),
        )

    if DAILY_FILE.exists():
        try:
            today = json.loads(DAILY_FILE.read_text())
            for sid, s in today.get("sessions", {}).items():
                add_session_day(
                    today.get("date", "?"), sid,
                    s.get("m", "?"), s.get("p", "?"), s.get("n", 0),
                    s.get("di", 0), s.get("do", 0), s.get("dc", 0), s.get("dcw", 0),
                )
        except Exception:
            pass

    rollup_lines = [
        json.dumps({
            "date": date_str,
            "input": d["input"],
            "output": d["output"],
            "cache_read": d["cache_read"],
            "cache_write": d["cache_write"],
            "sessions": d["sessions"],
            "machine": machine,
            "v": "interactive",
        })
        for date_str, d in sorted(by_date.items())
    ]

    _atomic_write_lines(JOURNAL_FILE, journal_lines)
    _atomic_write_lines(ROLLUP_FILE, rollup_lines)


if __name__ == "__main__":
    main()
