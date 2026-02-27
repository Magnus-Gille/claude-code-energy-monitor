#!/usr/bin/env python3
"""Claude Code Step Counter — shareable usage summaries.

Generates copy-pasteable usage visualizations from Claude Code
energy monitor data. Like a fitness tracker for your AI coding.

Usage:
    python stepcount.py              # today (default)
    python stepcount.py --week       # this week's heatmap
    python stepcount.py --month      # monthly calendar heatmap
    python stepcount.py --copy       # copy output to clipboard
"""

import argparse
import json
import math
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

CACHE_DIR = Path.home() / ".claude"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"

# Energy midpoints (mWh per 1k tokens) — geometric mean of ±3x bounds
# Source: Couch (2026), derived from Epoch AI + Anthropic pricing ratios
E_IN = 650       # fresh input
E_OUT = 3250     # output (decode)
E_CR = 65        # cache read
E_CW = 816.5     # cache write

# OOM scale: one relatable comparison per order of magnitude (Wh)
OOM_SCALE = [
    (1,        "a Google search"),
    (10,       "a phone charge"),
    (100,      "a laptop charge"),
    (1000,     "an hour of AC"),
    (10000,    "a day of home electricity"),
    (100000,   "a full EV charge"),
    (1000000,  "a month of home electricity"),
    (10000000, "a year of home electricity"),
]

DAYS_SHORT = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def load_days():
    """Load all days as {iso_date_str: day_dict}."""
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


def energy_wh(d):
    """Midpoint energy estimate in Wh for a day."""
    return (d.get("input", 0) / 1000 * E_IN
            + d.get("output", 0) / 1000 * E_OUT
            + d.get("cache_read", 0) / 1000 * E_CR
            + d.get("cache_write", 0) / 1000 * E_CW) / 1000


def total_tokens(d):
    return (d.get("input", 0) + d.get("output", 0)
            + d.get("cache_read", 0) + d.get("cache_write", 0))


def fmt_tok(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_energy(wh):
    """Format energy as order-of-magnitude estimate with 1/2/5 snap points."""
    mwh = wh * 1000
    if mwh < 1:
        return "~0"
    log = math.log10(mwh)
    decade = int(math.floor(log))
    frac = log - decade
    if frac < 0.15:
        val = 10 ** decade
    elif frac < 0.50:
        val = 2 * 10 ** decade
    elif frac < 0.85:
        val = 5 * 10 ** decade
    else:
        val = 10 ** (decade + 1)
    val = round(val)
    if val < 1000:
        return f"~{val}mWh"
    if val < 1_000_000:
        v = val / 1000
        return f"~{v:g}Wh"
    v = val / 1_000_000
    return f"~{v:g}kWh"


def energy_comparison(wh):
    """Single-line energy estimate with nearest real-world comparison."""
    if wh < 0.5:
        return "~0 Wh"
    user_str = fmt_energy(wh)
    for ref_wh, comparison in OOM_SCALE:
        ref_str = fmt_energy(ref_wh)
        if ref_str == user_str:
            return f"{user_str} ≈ {comparison} (±3×)"
        if ref_wh >= wh:
            break
    return f"{user_str} (±3×)"


def empty_day(date_str):
    return {"date": date_str, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0, "sessions": 0}


def aggregate(day_list):
    """Sum totals across multiple days."""
    tok = sum(total_tokens(d) for d in day_list)
    e = sum(energy_wh(d) for d in day_list)
    sess = sum(d.get("sessions", 0) for d in day_list)
    return tok, e, sess


# ── Views ──────────────────────────────────────────────────

def view_today(days):
    today = date.today()
    d = days.get(today.isoformat(), empty_day(today.isoformat()))

    tok = total_tokens(d)
    e = energy_wh(d)
    sess = d.get("sessions", 0)

    return (f"⚡ Claude Code · {today.strftime('%b')} {today.day}\n"
            f"{fmt_tok(tok)} tokens · {sess} sessions\n"
            f"{energy_comparison(e)}")


def view_week(days):
    today = date.today()
    week_dates = [today - timedelta(days=6 - i) for i in range(7)]

    week_data = [days.get(dt.isoformat(), empty_day(dt.isoformat()))
                 for dt in week_dates]
    tok, e, sess = aggregate(week_data)

    # Date range header
    start = week_dates[0]
    if start.month == today.month:
        range_str = f"{start.strftime('%b')} {start.day}–{today.day}"
    else:
        range_str = (f"{start.strftime('%b')} {start.day} – "
                     f"{today.strftime('%b')} {today.day}")

    return (f"⚡ Claude Code · {range_str}\n"
            f"{fmt_tok(tok)} tokens · {sess} sessions\n"
            f"{energy_comparison(e)}")


def view_month(days):
    today = date.today()
    month_dates = [today - timedelta(days=29 - i) for i in range(30)]

    month_data = [days.get(dt.isoformat(), empty_day(dt.isoformat()))
                  for dt in month_dates]
    tok, e, sess = aggregate(month_data)

    # Date range header
    start = month_dates[0]
    if start.month == today.month:
        range_str = f"{start.strftime('%b')} {start.day}–{today.day}"
    else:
        range_str = (f"{start.strftime('%b')} {start.day} – "
                     f"{today.strftime('%b')} {today.day}")

    return (f"⚡ Claude Code · {range_str}\n"
            f"{fmt_tok(tok)} tokens · {sess} sessions\n"
            f"{energy_comparison(e)}")


# ── Main ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🦶 Claude Code Step Counter — shareable usage summaries")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--week", action="store_true",
                       help="Last 7 days")
    group.add_argument("--month", action="store_true",
                       help="Last 30 days")
    parser.add_argument("--copy", action="store_true",
                        help="Copy to clipboard")
    args = parser.parse_args()

    days = load_days()
    if not days:
        print("No data found. Use Claude Code with the energy monitor first.",
              file=sys.stderr)
        sys.exit(1)

    if args.month:
        output = view_month(days)
    elif args.week:
        output = view_week(days)
    else:
        output = view_today(days)

    print(output)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=output.encode(), check=True)
            print("\n📋 Copied to clipboard!", file=sys.stderr)
        except Exception:
            print("\n⚠️  Could not copy to clipboard", file=sys.stderr)


if __name__ == "__main__":
    main()
