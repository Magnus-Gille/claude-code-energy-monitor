#!/usr/bin/env python3
"""Bar charts of daily token usage and estimated energy consumption.

Reads from ~/.claude/statusline_history.jsonl (completed days) and
~/.claude/statusline_daily.json (today, in progress).

Energy constants imported from energy_constants.py (single source of truth).

Usage:
    python plot_daily.py              # show in window
    python plot_daily.py -o out.png   # save to file
    python plot_daily.py --last 30    # only last N days
    python plot_daily.py --window 7   # trailing avg window in calendar days (default 7)
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    import matplotlib
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib required: pip install matplotlib", file=sys.stderr)
    sys.exit(1)

CACHE_DIR = Path.home() / ".claude"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"

from energy_constants import E_IN, E_OUT, E_CACHE, E_CW


def load_days():
    """Load completed days from history + today from daily file."""
    raw = []

    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            if line.strip():
                raw.append(json.loads(line))

    if DAILY_FILE.exists():
        today = json.loads(DAILY_FILE.read_text())
        raw.append({
            "date": today["date"],
            "input": today.get("input", 0),
            "output": today.get("output", 0),
            "cache_read": today.get("cached", 0),
            "cache_write": today.get("cache_write", 0),
        })

    return raw


def fill_gaps(raw):
    """Insert zero-rows for missing calendar days so the window is time-accurate."""
    if not raw:
        return []

    by_date = {r["date"]: r for r in raw}
    first = date.fromisoformat(raw[0]["date"])
    last = date.fromisoformat(raw[-1]["date"])

    result = []
    d = first
    while d <= last:
        key = d.isoformat()
        if key in by_date:
            result.append(by_date[key])
        else:
            result.append({"date": key, "input": 0, "output": 0,
                           "cache_read": 0, "cache_write": 0})
        d += timedelta(days=1)
    return result


def compute(days):
    """Return per-day arrays: dates, compute tokens (M), cache tokens (M), energy (Wh)."""
    dates, compute_tok, cache_tok, energy_wh = [], [], [], []
    for day in days:
        dates.append(day["date"][5:])  # MM-DD
        inp = day["input"]
        out = day["output"]
        cr  = day["cache_read"]
        cw  = day["cache_write"]

        compute_tok.append((inp + out) / 1e6)
        cache_tok.append((cr + cw) / 1e6)

        e_mwh = (inp / 1000 * E_IN
                 + cr  / 1000 * E_CACHE
                 + cw  / 1000 * E_CW
                 + out / 1000 * E_OUT)
        energy_wh.append(e_mwh / 1000)

    return dates, compute_tok, cache_tok, energy_wh


def trailing_avg(values, window):
    """Trailing (causal) average over the last `window` points. No future leakage."""
    result = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = values[lo:i + 1]
        result.append(sum(chunk) / len(chunk))
    return result


def plot(dates, compute_tok, cache_tok, energy_wh, output=None, window=7):
    """Stacked bar chart (compute vs cache tokens) + energy, with trailing avg overlay."""
    if output:
        matplotlib.use("Agg")

    total_tok = [c + k for c, k in zip(compute_tok, cache_tok)]
    tok_avg   = trailing_avg(total_tok, window)
    eng_avg   = trailing_avg(energy_wh, window)

    n = len(dates)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(max(10, n * 0.55), 9))
    x = range(n)

    # ── Token chart (stacked) ──────────────────────────────────────────────
    ax1.bar(x, cache_tok,   color="#a5b4fc", alpha=0.85, label="Cache (read+write)")
    ax1.bar(x, compute_tok, color="#4f46e5", alpha=0.85, bottom=cache_tok,
            label="Compute (input+output)")
    ax1.plot(x, tok_avg, color="#1e1b4b", linewidth=2.2, marker="o",
             markersize=3.5, label=f"{window}-day trailing avg", zorder=3)
    ax1.set_ylabel("Tokens (millions)")
    ax1.set_title("Daily Token Usage", fontweight="bold", fontsize=13)
    ax1.grid(axis="y", alpha=0.3)
    ax1.set_axisbelow(True)
    ax1.legend(fontsize=9)
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(dates)

    # ── Energy chart ───────────────────────────────────────────────────────
    ax2.bar(x, energy_wh, color="#f59e0b", alpha=0.75, label="Daily")
    ax2.plot(x, eng_avg, color="#78350f", linewidth=2.2, marker="o",
             markersize=3.5, label=f"{window}-day trailing avg", zorder=3)
    ax2.set_ylabel("Energy (Wh)")
    ax2.set_title("Daily Estimated Energy (Midpoint)", fontweight="bold", fontsize=13)
    ax2.set_xlabel("Date (2026)")
    ax2.grid(axis="y", alpha=0.3)
    ax2.set_axisbelow(True)
    ax2.legend(fontsize=9)
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(dates)

    for ax in (ax1, ax2):
        ax.tick_params(axis="x", rotation=45, labelsize=8)

    plt.tight_layout(pad=2)

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser(description="Plot daily Claude Code usage")
    parser.add_argument("-o", "--output", help="Save to file instead of showing")
    parser.add_argument("--last", type=int, help="Only show last N days")
    parser.add_argument("--window", type=int, default=7,
                        help="Trailing average window in calendar days (default: 7)")
    args = parser.parse_args()

    raw = load_days()
    if not raw:
        print("No data found.", file=sys.stderr)
        sys.exit(1)

    days = fill_gaps(raw)

    if args.last:
        days = days[-args.last:]

    dates, compute_tok, cache_tok, energy_wh = compute(days)
    plot(dates, compute_tok, cache_tok, energy_wh, output=args.output, window=args.window)


if __name__ == "__main__":
    main()
