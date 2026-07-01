#!/usr/bin/env python3
"""Regression test for the interactive_export.py double-counting bug.

A Codex review of the m5<->laptop full-mesh sync PR caught this: an earlier
version wrote the local export to `interactive_journal.jsonl`, which matches
advisor.py's `*_journal.jsonl` remote-merge glob, so a machine's own
interactive usage got counted twice (once from statusline_*, once from its
own glob-matched export). Fixed by renaming to `interactive_journal_raw.jsonl`
(deliberately outside the glob). This test proves it stays fixed, and that
a tagged remote copy is still merged correctly.

No test framework in this repo — plain assertions, run directly:
    python3 test_interactive_export.py
"""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def run():
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        claude_dir = Path(home) / ".claude"
        claude_dir.mkdir()

        # Simulate one archived interactive session-day: 10 in / 20 out /
        # 30 cache-read / 40 cache-write. Two files, mirroring what
        # statusline.py actually archives at day rollover: the day-level
        # total (what advisor.py's *native* local load reads) and the
        # per-session breakdown (what interactive_export.py reads/reconstructs
        # from). Populating only one of the two wouldn't reproduce the bug —
        # the double-count only shows up when both exist, same as production.
        _write_jsonl(claude_dir / "statusline_history.jsonl", [{
            "date": "2026-07-01", "input": 10, "output": 20,
            "cache_read": 30, "cache_write": 40, "sessions": 1,
        }])
        _write_jsonl(claude_dir / "statusline_session_history.jsonl", [{
            "date": "2026-07-01", "sid": "s1", "m": "claude-sonnet-5", "p": "proj",
            "n": 1, "di": 10, "do": 20, "dc": 30, "dcw": 40,
        }])

        # interactive_export.py and advisor.py both cache CLAUDE_DIR from
        # Path.home() at import time, so they must be (re-)imported only
        # after HOME is set, in this fresh-per-run process.
        interactive_export = importlib.import_module("interactive_export")
        advisor = importlib.import_module("advisor")

        interactive_export.main()

        raw_journal = claude_dir / "interactive_journal_raw.jsonl"
        raw_rollup = claude_dir / "interactive_rollup_raw.jsonl"
        assert raw_journal.exists(), "expected interactive_journal_raw.jsonl to be written"
        assert raw_rollup.exists(), "expected interactive_rollup_raw.jsonl to be written"

        glob_names = {f.name for f in advisor._remote_journal_files()}
        assert "interactive_journal_raw.jsonl" not in glob_names, (
            f"local raw export must NOT match the remote-merge glob, got: {glob_names}"
        )

        local_only = advisor.load_daily_history(include_remote=False).get("2026-07-01", {})
        merged = advisor.load_daily_history(include_remote=True).get("2026-07-01", {})
        assert local_only == merged, (
            "local interactive usage was double-counted when merging remote data: "
            f"local_only={local_only} merged={merged}"
        )
        assert merged.get("input") == 10 and merged.get("output") == 20, (
            f"expected exactly one copy of the session's tokens, got {merged}"
        )

        # A tagged REMOTE copy (as m5/pi would land on the laptop after sync)
        # must still be merged in — the fix should only exclude the local
        # untagged raw files, not remote tagged ones.
        _write_jsonl(claude_dir / "m5_interactive_daily_rollup.jsonl", [{
            "date": "2026-07-01", "input": 100, "output": 200,
            "cache_read": 300, "cache_write": 400, "sessions": 1,
            "machine": "m5", "v": "interactive",
        }])
        merged_with_remote = advisor.load_daily_history(include_remote=True).get("2026-07-01", {})
        assert merged_with_remote.get("input") == 110, (
            f"expected local (10) + tagged remote (100) = 110, got {merged_with_remote}"
        )

    print("OK: interactive_export.py output is excluded from local double-count, "
          "tagged remote copies still merge correctly.")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    run()
