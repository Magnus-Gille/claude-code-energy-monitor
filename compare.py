#!/usr/bin/env python3
"""Statusline vs JSONL token comparison table.

Shows side-by-side token counts from both data sources to highlight
where JSONL undercounts (input placeholders, missing thinking tokens)
and where it agrees (cache metrics).

Usage:
    python compare.py              # today (default)
    python compare.py --week       # last 7 days
    python compare.py --month      # last 30 days
    python compare.py --copy       # copy to clipboard
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
DAILY_FILE = CLAUDE_DIR / "statusline_daily.json"
HISTORY_FILE = CLAUDE_DIR / "statusline_history.jsonl"


# ── Data loading: statusline ────────────────────────────────

def load_statusline_days():
    """Load all statusline days as {iso_date: dict}."""
    days = {}
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                days[d["date"]] = d
    if DAILY_FILE.exists():
        today = json.loads(DAILY_FILE.read_text())
        days[today["date"]] = {
            "date": today["date"],
            "input": today.get("input", 0),
            "output": today.get("output", 0),
            "cache_read": today.get("cached", 0),
            "cache_write": today.get("cache_write", 0),
            "sessions": len(today.get("sessions", {})),
        }
    return days


# ── Data loading: JSONL ─────────────────────────────────────

def find_jsonl_files():
    """Find all session and subagent JSONL files."""
    if not PROJECTS_DIR.exists():
        return []
    return [f for f in PROJECTS_DIR.rglob("*.jsonl")
            if not f.name.startswith(".")]


def parse_jsonl(path):
    """Yield assistant messages with usage data from a JSONL file."""
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
                if entry.get("type") != "assistant":
                    continue
                usage = entry.get("message", {}).get("usage")
                if not usage:
                    continue
                yield {
                    "timestamp": entry.get("timestamp", ""),
                    "request_id": entry.get("requestId", ""),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                }
    except Exception as e:
        print(f"  Warning: {path}: {e}", file=sys.stderr)


def load_jsonl_days():
    """Load JSONL data, deduplicated by requestId, grouped by date."""
    raw_by_date = defaultdict(list)
    for fpath in find_jsonl_files():
        for msg in parse_jsonl(fpath):
            ts = msg["timestamp"]
            if ts:
                raw_by_date[ts[:10]].append(msg)

    days = {}
    for d, msgs in raw_by_date.items():
        # Deduplicate: last entry per requestId
        by_request = {}
        for m in msgs:
            rid = m["request_id"]
            if rid:
                by_request[rid] = m

        deduped = list(by_request.values())
        days[d] = {
            "date": d,
            "input": sum(m["input_tokens"] for m in deduped),
            "output": sum(m["output_tokens"] for m in deduped),
            "cache_read": sum(m["cache_read"] for m in deduped),
            "cache_write": sum(m["cache_creation"] for m in deduped),
            "requests": len(deduped),
            "raw_entries": len(msgs),
        }
    return days


# ── Formatting ──────────────────────────────────────────────

def fmt(n):
    """Format token count: 1.2M, 45k, 0."""
    if n == 0:
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def ratio_str(sl, jl):
    """Format ratio between statusline and JSONL values."""
    if jl == 0:
        return "n/a" if sl == 0 else "inf"
    r = sl / jl
    if 0.95 <= r <= 1.05:
        return f"{r:.2f}x"
    if r >= 10:
        return f"{r:.0f}x"
    return f"{r:.1f}x"


# ── Aggregation ─────────────────────────────────────────────

def empty_day(d):
    return {"date": d, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0,
            "requests": 0, "raw_entries": 0, "sessions": 0}


def sum_days(day_list):
    """Sum token fields across days."""
    return {
        "input": sum(d.get("input", 0) for d in day_list),
        "output": sum(d.get("output", 0) for d in day_list),
        "cache_read": sum(d.get("cache_read", 0) for d in day_list),
        "cache_write": sum(d.get("cache_write", 0) for d in day_list),
    }


def total(d):
    return d["input"] + d["output"] + d["cache_read"] + d["cache_write"]


# ── Table rendering ─────────────────────────────────────────

def render_table(sl, jl, label):
    """Render a comparison table for a date range."""
    rows = [
        ("Input", "input"),
        ("Output", "output"),
        ("Cache read", "cache_read"),
        ("Cache write", "cache_write"),
    ]

    sl_total = total(sl)
    jl_total = total(jl)

    # Column widths
    lbl_w = 12
    val_w = 10
    rat_w = 8

    header = f"{'':>{lbl_w}}  {'Statusline':>{val_w}}  {'JSONL':>{val_w}}  {'Ratio':>{rat_w}}"
    sep = f"{'':>{lbl_w}}  {'─' * val_w}  {'─' * val_w}  {'─' * rat_w}"

    lines = []
    lines.append(f"Statusline vs JSONL · {label}")
    lines.append("")
    lines.append(header)
    lines.append(sep)

    for name, key in rows:
        sv = sl[key]
        jv = jl[key]
        r = ratio_str(sv, jv)
        lines.append(f"{name:>{lbl_w}}  {fmt(sv):>{val_w}}  {fmt(jv):>{val_w}}  {r:>{rat_w}}")

    lines.append(sep)
    r = ratio_str(sl_total, jl_total)
    lines.append(f"{'Total':>{lbl_w}}  {fmt(sl_total):>{val_w}}  {fmt(jl_total):>{val_w}}  {r:>{rat_w}}")

    # Notes on discrepancies
    notes = []
    if jl["input"] > 0 and sl["input"] / jl["input"] > 5:
        notes.append("Input: JSONL records placeholder (1), not real token counts")
    if jl["output"] > 0 and sl["output"] / jl["output"] > 2:
        notes.append("Output: JSONL excludes thinking tokens")
    cr_r = sl["cache_read"] / jl["cache_read"] if jl["cache_read"] > 0 else 1
    if 0.7 <= cr_r <= 1.5:
        notes.append(f"Cache read: both sources agree ({ratio_str(sl['cache_read'], jl['cache_read'])})")

    if notes:
        lines.append("")
        for n in notes:
            lines.append(f"  {n}")

    return "\n".join(lines)


# ── Views ───────────────────────────────────────────────────

def get_dates_today():
    return [date.today()]


def get_dates_week():
    today = date.today()
    return [today - timedelta(days=6 - i) for i in range(7)]


def get_dates_month():
    today = date.today()
    return [today - timedelta(days=29 - i) for i in range(30)]


def date_label(dates):
    """Format a date range label."""
    if len(dates) == 1:
        d = dates[0]
        return f"{d.strftime('%b')} {d.day}"
    start, end = dates[0], dates[-1]
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}\u2013{end.day}"
    return f"{start.strftime('%b')} {start.day} \u2013 {end.strftime('%b')} {end.day}"


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Statusline vs JSONL token comparison")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--week", action="store_true", help="Last 7 days")
    group.add_argument("--month", action="store_true", help="Last 30 days")
    parser.add_argument("--copy", action="store_true",
                        help="Copy to clipboard")
    args = parser.parse_args()

    # Pick date range
    if args.month:
        dates = get_dates_month()
    elif args.week:
        dates = get_dates_week()
    else:
        dates = get_dates_today()

    label = date_label(dates)
    date_strs = {d.isoformat() for d in dates}

    # Load both sources
    sl_days = load_statusline_days()
    print("Scanning JSONL files...", file=sys.stderr)
    jl_days = load_jsonl_days()
    print(f"Found data for {len(jl_days)} days in JSONL", file=sys.stderr)

    # Aggregate over date range
    sl_range = [sl_days.get(d, empty_day(d)) for d in sorted(date_strs)]
    jl_range = [jl_days.get(d, empty_day(d)) for d in sorted(date_strs)]

    sl_agg = sum_days(sl_range)
    jl_agg = sum_days(jl_range)

    output = render_table(sl_agg, jl_agg, label)
    print(output)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=output.encode(), check=True)
            print("\nCopied to clipboard!", file=sys.stderr)
        except Exception:
            print("\nCould not copy to clipboard", file=sys.stderr)


if __name__ == "__main__":
    main()
