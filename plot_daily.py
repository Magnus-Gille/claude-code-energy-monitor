#!/usr/bin/env python3
"""Bar charts of daily token usage and estimated energy consumption.

Reads from ~/.claude/statusline_history.jsonl (completed days) and
~/.claude/statusline_daily.json (today, in progress).

Energy constants imported from energy_constants.py (single source of truth).

Usage:
    python plot_daily.py              # show in window
    python plot_daily.py -o out.png   # save to file
    python plot_daily.py --last 7     # only last N days
"""

import argparse
import json
import sys
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

# Energy constants: mWh per 1k tokens (mid estimates).
# Imported from energy_constants.py — the single source of truth.
from energy_constants import E_IN, E_OUT, E_CACHE, E_CW


def load_days():
    """Load completed days from history + today from daily file."""
    days = []

    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            if line.strip():
                days.append(json.loads(line))

    if DAILY_FILE.exists():
        today = json.loads(DAILY_FILE.read_text())
        days.append({
            "date": today["date"],
            "input": today.get("input", 0),
            "output": today.get("output", 0),
            "cache_read": today.get("cached", 0),
            "cache_write": today.get("cache_write", 0),
            "sessions": len(today.get("sessions", {})),
        })

    return days


def compute(days):
    """Compute total tokens (M) and energy (Wh) per day."""
    dates, tokens, energy_wh = [], [], []
    for day in days:
        dates.append(day["date"][5:])  # MM-DD
        inp = day["input"]
        out = day["output"]
        cr = day["cache_read"]
        cw = day["cache_write"]

        tokens.append((inp + out + cr + cw) / 1e6)

        e_mwh = (inp / 1000 * E_IN
                 + cr / 1000 * E_CACHE
                 + cw / 1000 * E_CW
                 + out / 1000 * E_OUT)
        energy_wh.append(e_mwh / 1000)

    return dates, tokens, energy_wh


def plot(dates, tokens, energy_wh, output=None):
    """Render two bar charts: tokens and energy."""
    if output:
        matplotlib.use("Agg")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(max(10, len(dates) * 1.4), 8))

    # Tokens
    bars1 = ax1.bar(dates, tokens, color="#6366f1", alpha=0.85,
                    edgecolor="white", linewidth=0.5)
    ax1.set_ylabel("Tokens (millions)")
    ax1.set_title("Daily Token Usage", fontweight="bold", fontsize=13)
    ax1.grid(axis="y", alpha=0.3)
    ax1.set_axisbelow(True)
    for bar, val in zip(bars1, tokens):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(tokens) * 0.01,
                 f"{val:.0f}M", ha="center", va="bottom", fontsize=9, fontweight="bold")

    # Energy
    bars2 = ax2.bar(dates, energy_wh, color="#f59e0b", alpha=0.85,
                    edgecolor="white", linewidth=0.5)
    ax2.set_ylabel("Energy (Wh)")
    ax2.set_title("Daily Estimated Energy Use (Midpoint)", fontweight="bold", fontsize=13)
    ax2.set_xlabel("Date (2026)")
    ax2.grid(axis="y", alpha=0.3)
    ax2.set_axisbelow(True)
    for bar, val in zip(bars2, energy_wh):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(energy_wh) * 0.01,
                 f"{val:.1f}", ha="center", va="bottom", fontsize=9, fontweight="bold")

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
    args = parser.parse_args()

    days = load_days()
    if not days:
        print("No data found.", file=sys.stderr)
        sys.exit(1)

    if args.last:
        days = days[-args.last:]

    dates, tokens, energy_wh = compute(days)
    plot(dates, tokens, energy_wh, output=args.output)


if __name__ == "__main__":
    main()
