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

# Real-world comparisons, ordered by magnitude: (wh_per_unit, singular, plural)
COMPARISONS = [
    (0.3, "Google search", "Google searches"),
    (15, "phone charge", "phone charges"),
    (60, "laptop charge", "laptop charges"),
    (150, "km of EV driving", "km of EV driving"),
    (1000, "hour of air conditioning", "hours of air conditioning"),
]

HEAT = ["⬜", "🟨", "🟧", "🟥"]
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


def pick_comparison(wh):
    """Auto-select the real-world comparison that gives a human-sized number."""
    if wh < 0.01:
        return ""
    best = COMPARISONS[0]
    for comp in COMPARISONS:
        if wh / comp[0] >= 1:
            best = comp
    count = wh / best[0]
    _, singular, plural = best
    if count < 1.05:
        return f"≈ 1 {singular}"
    if count < 10:
        return f"≈ {count:.1f} {plural}"
    return f"≈ {count:.0f} {plural}"


def compute_thresholds(token_counts):
    """Tercile boundaries from non-zero values for 3-level heatmap."""
    nonzero = sorted(v for v in token_counts if v > 0)
    if not nonzero:
        return (1, 2)
    if len(nonzero) == 1:
        v = nonzero[0]
        return (v * 0.33, v * 0.66)
    if len(nonzero) == 2:
        return (nonzero[0], (nonzero[0] + nonzero[1]) / 2)
    t1 = nonzero[len(nonzero) // 3]
    t2 = nonzero[len(nonzero) * 2 // 3]
    return (t1, t2)


def heat_cell(value, thresholds):
    """Map a token count to a heatmap emoji."""
    if value == 0:
        return HEAT[0]
    t1, t2 = thresholds
    if value <= t1:
        return HEAT[1]
    if value <= t2:
        return HEAT[2]
    return HEAT[3]


def empty_day(date_str):
    return {"date": date_str, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0, "sessions": 0}


def aggregate(day_list):
    """Sum totals across multiple days."""
    tok = sum(total_tokens(d) for d in day_list)
    e = sum(energy_wh(d) for d in day_list)
    sess = sum(d.get("sessions", 0) for d in day_list)
    return tok, e, sess


def summary_lines(tok, e, sess):
    """Format the summary footer."""
    comp = pick_comparison(e)
    lines = [f"   {fmt_tok(tok)} tokens · {sess} sessions"]
    energy_str = f"   ⚡ {fmt_energy(e)}"
    if comp:
        energy_str += f" {comp}"
    lines.append(energy_str)
    lines.append("   (order-of-magnitude estimate · ±3×)")
    return lines


# ── Views ──────────────────────────────────────────────────

def view_today(days):
    today = date.today()
    d = days.get(today.isoformat(), empty_day(today.isoformat()))

    tok = total_tokens(d)
    e = energy_wh(d)
    sess = d.get("sessions", 0)

    lines = [
        f"🦶 Claude Code · {today.strftime('%b')} {today.day}",
        "",
    ]
    lines.extend(summary_lines(tok, e, sess))
    return "\n".join(lines)


def view_week(days):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    week_data = []
    for dt in week_dates:
        week_data.append(days.get(dt.isoformat(), empty_day(dt.isoformat())))

    token_counts = [total_tokens(d) for d in week_data]
    thresholds = compute_thresholds(token_counts)

    tok, e, sess = aggregate(week_data)

    # Date range header
    end = min(week_dates[-1], today)
    if monday.month == end.month:
        range_str = f"{monday.strftime('%b')} {monday.day}–{end.day}"
    else:
        range_str = (f"{monday.strftime('%b')} {monday.day} – "
                     f"{end.strftime('%b')} {end.day}")

    # Heatmap row
    cells = []
    for i in range(7):
        if week_dates[i] > today:
            cells.append(f"{DAYS_SHORT[i]} ⬜")
        else:
            cells.append(
                f"{DAYS_SHORT[i]} {heat_cell(token_counts[i], thresholds)}")
    heatmap = "  ".join(cells)

    lines = [
        f"🦶 Claude Code · {range_str}",
        "",
        f"   {heatmap}",
        "",
    ]
    lines.extend(summary_lines(tok, e, sess))
    return "\n".join(lines)


def view_month(days):
    today = date.today()
    first = today.replace(day=1)

    # All dates in the month up to today
    month_dates = []
    dt = first
    while dt.month == first.month and dt <= today:
        month_dates.append(dt)
        dt += timedelta(days=1)

    month_data = [days.get(d.isoformat(), empty_day(d.isoformat()))
                  for d in month_dates]

    token_counts = [total_tokens(d) for d in month_data]
    thresholds = compute_thresholds(token_counts)

    tok, e, sess = aggregate(month_data)

    # Calendar grid: rows = weekdays (Mo–Su), cols = week columns
    first_weekday = first.weekday()
    week_start = first - timedelta(days=first_weekday)
    last_date = month_dates[-1]
    num_weeks = ((last_date - week_start).days // 7) + 1

    grid = [["  "] * num_weeks for _ in range(7)]
    for dt in month_dates:
        w = (dt - week_start).days // 7
        d = days.get(dt.isoformat(), empty_day(dt.isoformat()))
        grid[dt.weekday()][w] = heat_cell(total_tokens(d), thresholds)

    lines = [
        f"🦶 Claude Code · {first.strftime('%B %Y')}",
        "",
    ]
    for day_idx in range(7):
        row = f"   {DAYS_SHORT[day_idx]}  " + "  ".join(grid[day_idx])
        lines.append(row.rstrip())

    lines.append("")
    lines.extend(summary_lines(tok, e, sess))
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🦶 Claude Code Step Counter — shareable usage summaries")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--week", action="store_true",
                       help="This week's heatmap")
    group.add_argument("--month", action="store_true",
                       help="Monthly calendar heatmap")
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
