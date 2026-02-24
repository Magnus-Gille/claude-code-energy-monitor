#!/usr/bin/env python3
"""Independent JSONL token summer — no ccusage dependency.

Reads Claude Code's raw JSONL conversation logs and sums token usage
from assistant messages. Includes subagent files.

Usage:
  python3 sum_jsonl.py                    # today
  python3 sum_jsonl.py 2026-02-20         # specific date
  python3 sum_jsonl.py 2026-02-20 --all   # all dates in that project dir

Compares against statusline_history.jsonl / statusline_daily.json
if available.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
DAILY_FILE = CLAUDE_DIR / "statusline_daily.json"
HISTORY_FILE = CLAUDE_DIR / "statusline_history.jsonl"


def find_jsonl_files():
    """Find all session and subagent JSONL files."""
    files = []
    if not PROJECTS_DIR.exists():
        return files

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        # Skip non-conversation files
        if jsonl.name.startswith("."):
            continue
        files.append(jsonl)

    return files


def parse_jsonl(path):
    """Parse a single JSONL file, yielding assistant messages with usage data."""
    try:
        with open(path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue

                msg = entry.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue

                timestamp = entry.get("timestamp", "")
                session_id = entry.get("sessionId", "unknown")
                is_subagent = entry.get("isSidechain", False)
                request_id = entry.get("requestId", "")

                yield {
                    "timestamp": timestamp,
                    "session_id": session_id,
                    "request_id": request_id,
                    "is_subagent": is_subagent,
                    "file": str(path),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                }
    except Exception as e:
        print(f"  Warning: could not read {path}: {e}", file=sys.stderr)


def load_statusline_history():
    """Load statusline history for comparison."""
    history = {}
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        d = entry.get("date")
                        if d:
                            history[d] = entry
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    if DAILY_FILE.exists():
        try:
            daily = json.loads(DAILY_FILE.read_text())
            d = daily.get("date")
            if d:
                history[d] = {
                    "date": d,
                    "input": daily.get("input", 0),
                    "output": daily.get("output", 0),
                    "cache_read": daily.get("cached", 0),
                    "cache_write": daily.get("cache_write", 0),
                    "sessions": len(daily.get("sessions", {})),
                }
        except Exception:
            pass

    return history


def main():
    target_date = None
    show_all = "--all" in sys.argv

    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            continue
        try:
            target_date = arg
            datetime.strptime(arg, "%Y-%m-%d")  # validate format
        except ValueError:
            print(f"Invalid date format: {arg}. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    if not target_date and not show_all:
        target_date = date.today().isoformat()

    print(f"Scanning JSONL files in {PROJECTS_DIR}...")
    all_files = find_jsonl_files()
    print(f"Found {len(all_files)} JSONL files")

    # Count subagent files
    subagent_files = [f for f in all_files if "subagents" in str(f)]
    main_files = [f for f in all_files if "subagents" not in str(f)]
    print(f"  Main session files: {len(main_files)}")
    print(f"  Subagent files: {len(subagent_files)}")

    # Parse all files, collect per-request entries for dedup
    raw_by_date = defaultdict(list)

    for fpath in all_files:
        for msg in parse_jsonl(fpath):
            ts = msg["timestamp"]
            if not ts:
                continue
            try:
                msg_date = ts[:10]  # YYYY-MM-DD from ISO timestamp
            except (IndexError, TypeError):
                continue
            if target_date and not show_all and msg_date != target_date:
                continue
            raw_by_date[msg_date].append(msg)

    if not raw_by_date:
        print(f"\nNo JSONL messages found for {target_date or 'any date'}.")
        return

    # Load statusline history for comparison
    sl_history = load_statusline_history()

    # Print results
    for d in sorted(raw_by_date.keys()):
        msgs = raw_by_date[d]

        # Raw (naive) sums
        raw_in = sum(m["input_tokens"] for m in msgs)
        raw_out = sum(m["output_tokens"] for m in msgs)
        raw_cr = sum(m["cache_read"] for m in msgs)
        raw_cc = sum(m["cache_creation"] for m in msgs)
        sessions = set(m["session_id"] for m in msgs)
        subagent_count = sum(1 for m in msgs if m["is_subagent"])

        # Deduplicated: last entry per requestId (streaming duplicates removed)
        by_request = {}
        for m in msgs:
            rid = m["request_id"]
            if rid:
                by_request[rid] = m  # last entry wins

        dedup_in = sum(m["input_tokens"] for m in by_request.values())
        dedup_out = sum(m["output_tokens"] for m in by_request.values())
        dedup_cr = sum(m["cache_read"] for m in by_request.values())
        dedup_cc = sum(m["cache_creation"] for m in by_request.values())

        # Count how many requests have placeholder input (<=1)
        placeholder_input = sum(1 for m in by_request.values() if m["input_tokens"] <= 1)
        placeholder_output = sum(1 for m in by_request.values() if m["output_tokens"] <= 1)

        print(f"\n{'='*72}")
        print(f"Date: {d}")
        print(f"{'='*72}")
        print(f"  Raw JSONL entries:   {len(msgs):,} ({subagent_count:,} from subagents)")
        print(f"  Unique API requests: {len(by_request):,} (after dedup by requestId)")
        print(f"  Sessions:            {len(sessions)}")
        dup_pct = (1 - len(by_request) / len(msgs)) * 100 if msgs else 0
        print(f"  Streaming duplicates: {len(msgs) - len(by_request):,} ({dup_pct:.0f}% of entries)")
        print()

        print(f"  Placeholder values (after dedup):")
        print(f"    input_tokens <= 1:  {placeholder_input}/{len(by_request)} "
              f"({placeholder_input/len(by_request)*100:.0f}%)")
        print(f"    output_tokens <= 1: {placeholder_output}/{len(by_request)} "
              f"({placeholder_output/len(by_request)*100:.0f}%)")
        print()

        print(f"  Token totals (deduplicated — last entry per request):")
        print(f"    Input tokens:          {dedup_in:>12,}")
        print(f"    Output tokens:         {dedup_out:>12,}")
        print(f"    Cache read:            {dedup_cr:>12,}")
        print(f"    Cache creation:        {dedup_cc:>12,}")

        # Compare with statusline if available
        sl = sl_history.get(d)
        if sl:
            sl_in = sl.get("input", 0)
            sl_out = sl.get("output", 0)
            sl_cr = sl.get("cache_read", 0)
            sl_cw = sl.get("cache_write", 0)

            in_ratio = sl_in / dedup_in if dedup_in > 0 else float("inf")
            out_ratio = sl_out / dedup_out if dedup_out > 0 else float("inf")
            cr_ratio = sl_cr / dedup_cr if dedup_cr > 0 else float("inf")
            cc_ratio = sl_cw / dedup_cc if dedup_cc > 0 else float("inf")

            print(f"\n  Statusline totals:")
            print(f"    Input tokens:          {sl_in:>12,}")
            print(f"    Output tokens:         {sl_out:>12,}")
            print(f"    Cache read:            {sl_cr:>12,}")
            print(f"    Cache write:           {sl_cw:>12,}")

            print(f"\n  Ratios (statusline / JSONL dedup):")
            print(f"    Input:       {in_ratio:>8.1f}x")
            print(f"    Output:      {out_ratio:>8.1f}x")
            print(f"    Cache read:  {cr_ratio:>8.1f}x")
            print(f"    Cache write: {cc_ratio:>8.1f}x")

            print(f"\n  INTERPRETATION:")
            if 0.7 <= cr_ratio <= 1.5:
                print(f"    Cache read ~{cr_ratio:.1f}x  => Both sources tracking same API calls for cache")
            if in_ratio > 5:
                print(f"    Input {in_ratio:.0f}x   => JSONL usage.input_tokens are mostly placeholders (1)")
                print(f"                     JSONL never records real fresh input token counts")
            if out_ratio > 2:
                print(f"    Output {out_ratio:.1f}x => Gap likely = thinking tokens + missing intermediate calls")
        else:
            print(f"\n  (No statusline data available for {d})")


if __name__ == "__main__":
    main()
