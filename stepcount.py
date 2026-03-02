#!/usr/bin/env python3
"""Claude Code Step Counter — shareable usage summaries.

Generates copy-pasteable usage visualizations from Claude Code
energy monitor data. Like a fitness tracker for your AI coding.

Usage:
    python stepcount.py              # today + week + month stacked (default)
    python stepcount.py -d           # today only
    python stepcount.py -w           # last 7 days only
    python stepcount.py -m           # last 30 days only
    python stepcount.py -t           # today + week + month as table
    python stepcount.py --rough-energy-estimate  # add energy guess
    python stepcount.py --copy       # copy output to clipboard
"""

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

CACHE_DIR = Path.home() / ".claude"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"

# Energy constants: mWh per 1k tokens (mid estimates).
# Imported from energy_constants.py — the single source of truth.
from energy_constants import E_IN, E_OUT, E_CACHE, E_CW

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
            + d.get("cache_read", 0) / 1000 * E_CACHE
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


def _data_span(days):
    """Number of days from earliest recorded day to today (inclusive)."""
    if not days:
        return 0
    earliest = min(days.keys())
    return (date.today() - date.fromisoformat(earliest)).days + 1


def _gather_all(days):
    """Gather D/W/M data for combined views. Only include periods with full data."""
    today = date.today()
    span = _data_span(days)

    d_today = days.get(today.isoformat(), empty_day(today.isoformat()))
    tok_d = total_tokens(d_today)
    sess_d = d_today.get("sessions", 0)
    e_d = energy_wh(d_today)

    rows = [("Today", tok_d, sess_d, e_d)]

    if span >= 7:
        week_dates = [today - timedelta(days=6 - i) for i in range(7)]
        week_data = [days.get(dt.isoformat(), empty_day(dt.isoformat()))
                     for dt in week_dates]
        tok_w, e_w, sess_w = aggregate(week_data)
        rows.append(("Week", tok_w, sess_w, e_w))

    if span >= 30:
        month_dates = [today - timedelta(days=29 - i) for i in range(30)]
        month_data = [days.get(dt.isoformat(), empty_day(dt.isoformat()))
                      for dt in month_dates]
        tok_m, e_m, sess_m = aggregate(month_data)
        rows.append(("Month", tok_m, sess_m, e_m))

    return rows


# ── Views ──────────────────────────────────────────────────

def view_today(days, energy=False):
    today = date.today()
    d = days.get(today.isoformat(), empty_day(today.isoformat()))

    tok = total_tokens(d)
    sess = d.get("sessions", 0)
    out = (f"⚡ Claude Code · {today.strftime('%b')} {today.day}\n"
           f"{fmt_tok(tok)} tokens · {sess} sessions")
    if energy:
        out += f"\n{energy_comparison(energy_wh(d))}"
    return out


def view_week(days, energy=False):
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

    out = (f"⚡ Claude Code · {range_str}\n"
           f"{fmt_tok(tok)} tokens · {sess} sessions")
    if energy:
        out += f"\n{energy_comparison(e)}"
    return out


def view_month(days, energy=False):
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

    out = (f"⚡ Claude Code · {range_str}\n"
           f"{fmt_tok(tok)} tokens · {sess} sessions")
    if energy:
        out += f"\n{energy_comparison(e)}"
    return out


def view_all(days, energy=False):
    """Compact D/W/M stacked view."""
    rows = _gather_all(days)
    tok_strs = [fmt_tok(tok) for _, tok, _, _ in rows]
    tw = max(len(s) for s in tok_strs)
    sw = max(len(str(sess)) for _, _, sess, _ in rows)

    lines = ["⚡ Claude Code"]
    if energy:
        e_strs = [fmt_energy(e) for _, _, _, e in rows]
        ew = max(len(s) for s in e_strs)
        for (label, _, sess, _), tok_s, e_s in zip(rows, tok_strs, e_strs):
            lines.append(
                f"   {label:<5} {tok_s:>{tw}} tokens · "
                f"{sess:>{sw}} sessions · {e_s:>{ew}}")
    else:
        for (label, _, sess, _), tok_s in zip(rows, tok_strs):
            lines.append(
                f"   {label:<5} {tok_s:>{tw}} tokens · "
                f"{sess:>{sw}} sessions")

    return "\n".join(lines)


def view_table(days, energy=False):
    """ASCII table D/W/M with bars."""
    rows = _gather_all(days)
    tok_strs = [fmt_tok(tok) for _, tok, _, _ in rows]
    max_tok = max(tok for _, tok, _, _ in rows) or 1
    BAR_W = 10

    def bar(val):
        filled = round(val / max_tok * BAR_W)
        if val > 0 and filled == 0:
            filled = 1
        return "█" * filled + "░" * (BAR_W - filled)

    tw = max(max(len(s) for s in tok_strs), 6)   # "tokens"
    sw = max(max(len(str(s)) for _, _, s, _ in rows), 4)  # "sess"

    lines = ["⚡ Claude Code"]
    lines.append(f"   ┌{'─' * 7}┬{'─' * (tw + 2)}┬{'─' * (sw + 2)}┬{'─' * (BAR_W + 2)}┐")
    lines.append(f"   │       │ {'tokens':>{tw}} │ {'sess':>{sw}} │ {' ' * BAR_W} │")
    lines.append(f"   ├{'─' * 7}┼{'─' * (tw + 2)}┼{'─' * (sw + 2)}┼{'─' * (BAR_W + 2)}┤")
    for (label, tok, sess, _), tok_s in zip(rows, tok_strs):
        lines.append(
            f"   │ {label:<5} │ {tok_s:>{tw}} │ {sess:>{sw}} │ {bar(tok)} │")
    lines.append(f"   └{'─' * 7}┴{'─' * (tw + 2)}┴{'─' * (sw + 2)}┴{'─' * (BAR_W + 2)}┘")

    if energy:
        _, _, _, e_last = rows[-1]
        lines.append(f"   {energy_comparison(e_last)}")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────

# Common wrong flags → (suggestion, explanation)
_SUGGESTIONS = {
    "-a":        ("(no flag needed)", "all periods is the default"),
    "--all":     ("(no flag needed)", "all periods is the default"),
    "--weekly":  ("-w / --week",      None),
    "--monthly": ("-m / --month",     None),
    "-e":        ("--rough-energy-estimate", None),
    "--energy":  ("--rough-energy-estimate", None),
    "-c":        ("--copy",           None),
}

EXAMPLES = """\
examples:
  stepcount.py                 today + week + month (default)
  stepcount.py -d              today only
  stepcount.py -w              last 7 days only
  stepcount.py -m              last 30 days only
  stepcount.py -t              today + week + month as table
  stepcount.py --copy          copy output to clipboard
  stepcount.py --rough-energy-estimate   add energy estimate"""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that suggests corrections for common wrong flags."""

    def error(self, message):
        # Extract the bad flag from argparse's error message
        m = re.search(r"unrecognized arguments: (\S+)", message)
        if m:
            bad = m.group(1)
            if bad in _SUGGESTIONS:
                hint, note = _SUGGESTIONS[bad]
                msg = f"unknown flag: {bad}  →  did you mean: {hint}"
                if note:
                    msg += f"  ({note})"
                print(msg, file=sys.stderr)
                sys.exit(2)
        super().error(message)


def main():
    parser = _Parser(
        description="⚡ Claude Code Step Counter — shareable usage summaries",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-d", "--day", action="store_true",
                       help="Today only")
    group.add_argument("-w", "--week", action="store_true",
                       help="Last 7 days only")
    group.add_argument("-m", "--month", action="store_true",
                       help="Last 30 days only")
    group.add_argument("-t", "--table", action="store_true",
                       help="Show today, week, and month as ASCII table")
    parser.add_argument("--rough-energy-estimate", action="store_true",
                        help="Include order-of-magnitude energy guess (±3x)")
    parser.add_argument("--copy", action="store_true",
                        help="Copy output to clipboard")
    args = parser.parse_args()

    days = load_days()
    if not days:
        print("No data found. Use Claude Code with the energy monitor first.",
              file=sys.stderr)
        sys.exit(1)

    e = args.rough_energy_estimate
    if args.table:
        output = view_table(days, energy=e)
    elif args.month:
        output = view_month(days, energy=e)
    elif args.week:
        output = view_week(days, energy=e)
    elif args.day:
        output = view_today(days, energy=e)
    else:
        output = view_all(days, energy=e)

    print(output)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=output.encode(), check=True)
            print("\n📋 Copied to clipboard!", file=sys.stderr)
        except Exception:
            print("\n⚠️  Could not copy to clipboard", file=sys.stderr)


if __name__ == "__main__":
    main()
