#!/usr/bin/env python3
"""Codex Step Counter — shareable usage summaries from rollout logs."""

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

from codex_status import SESSIONS_DIR, build_payload, fmt_tok

OOM_SCALE = [
    (1, "a Google search"),
    (10, "a phone charge"),
    (100, "a laptop charge"),
    (1000, "an hour of AC"),
    (10000, "a day of home electricity"),
    (100000, "a full EV charge"),
    (1000000, "a month of home electricity"),
    (10000000, "a year of home electricity"),
]


def fmt_energy(wh: float) -> str:
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
        return f"~{val / 1000:g}Wh"
    return f"~{val / 1_000_000:g}kWh"


def energy_comparison(wh: float) -> str:
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


def period_rows(root: Path):
    payload = build_payload(root, None)
    return [
        ("Today", payload["day"]),
        ("Week", payload["week"]),
        ("Month", payload["month"]),
    ]


def view_period(root: Path, period_key: str, title: str, energy: bool) -> str:
    payload = build_payload(root, None)
    period = payload[period_key]
    tokens = fmt_tok(int(period["total_tokens"]))
    sessions = int(period["sessions"])
    out = f"⚡ Codex · {title}\n{tokens} tokens · {sessions} sessions"
    if energy:
        out += f"\n{energy_comparison(float(period['energy_mwh']) / 1000)}"
    return out


def view_all(root: Path, energy: bool) -> str:
    rows = period_rows(root)
    tok_strs = [fmt_tok(int(period["total_tokens"])) for _, period in rows]
    tw = max(len(s) for s in tok_strs)
    sw = max(len(str(int(period["sessions"]))) for _, period in rows)

    lines = ["⚡ Codex"]
    if energy:
        e_strs = [fmt_energy(float(period["energy_mwh"]) / 1000) for _, period in rows]
        ew = max(len(s) for s in e_strs)
        for (label, period), tok_s, e_s in zip(rows, tok_strs, e_strs):
            lines.append(
                f"   {label:<5} {tok_s:>{tw}} tokens · "
                f"{int(period['sessions']):>{sw}} sessions · {e_s:>{ew}}"
            )
    else:
        for (label, period), tok_s in zip(rows, tok_strs):
            lines.append(
                f"   {label:<5} {tok_s:>{tw}} tokens · "
                f"{int(period['sessions']):>{sw}} sessions"
            )
    return "\n".join(lines)


def view_table(root: Path, energy: bool) -> str:
    rows = period_rows(root)
    tok_vals = [int(period["total_tokens"]) for _, period in rows]
    tok_strs = [fmt_tok(v) for v in tok_vals]
    max_tok = max(tok_vals) or 1
    bar_w = 10

    def bar(val: int) -> str:
        filled = round(val / max_tok * bar_w)
        if val > 0 and filled == 0:
            filled = 1
        return "█" * filled + "░" * (bar_w - filled)

    tw = max(max(len(s) for s in tok_strs), 6)
    sw = max(max(len(str(int(period["sessions"]))) for _, period in rows), 4)

    lines = ["⚡ Codex"]
    lines.append(f"   ┌{'─' * 7}┬{'─' * (tw + 2)}┬{'─' * (sw + 2)}┬{'─' * (bar_w + 2)}┐")
    lines.append(f"   │       │ {'tokens':>{tw}} │ {'sess':>{sw}} │ {' ' * bar_w} │")
    lines.append(f"   ├{'─' * 7}┼{'─' * (tw + 2)}┼{'─' * (sw + 2)}┼{'─' * (bar_w + 2)}┤")
    for (label, period), tok_s, tok_v in zip(rows, tok_strs, tok_vals):
        lines.append(
            f"   │ {label:<5} │ {tok_s:>{tw}} │ {int(period['sessions']):>{sw}} │ {bar(tok_v)} │"
        )
    lines.append(f"   └{'─' * 7}┴{'─' * (tw + 2)}┴{'─' * (sw + 2)}┴{'─' * (bar_w + 2)}┘")

    if energy:
        lines.append(f"   {energy_comparison(float(rows[-1][1]['energy_mwh']) / 1000)}")
    return "\n".join(lines)


_SUGGESTIONS = {
    "-a": ("(no flag needed)", "all periods is the default"),
    "--all": ("(no flag needed)", "all periods is the default"),
    "--weekly": ("-w / --week", None),
    "--monthly": ("-m / --month", None),
    "-e": ("--rough-energy-estimate", None),
    "--energy": ("--rough-energy-estimate", None),
    "-c": ("--copy", None),
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
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
    parser = _Parser(description="⚡ Codex Step Counter — shareable usage summaries")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-d", "--day", action="store_true", help="Today only")
    group.add_argument("-w", "--week", action="store_true", help="Last 7 days only")
    group.add_argument("-m", "--month", action="store_true", help="Last 30 days only")
    group.add_argument("-t", "--table", action="store_true", help="Show today, week, and month as ASCII table")
    parser.add_argument("--rough-energy-estimate", action="store_true", help="Include order-of-magnitude energy guess (±3x)")
    parser.add_argument("--copy", action="store_true", help="Copy output to clipboard")
    parser.add_argument("--root", type=Path, default=SESSIONS_DIR, help="Codex sessions root (default: ~/.codex/sessions)")
    args = parser.parse_args()

    root = args.root.expanduser()
    payload = build_payload(root, None)
    if all(int(payload[key]["total_tokens"]) == 0 for key in ("day", "week", "month")):
        print("No Codex rollout data found. Use Codex without --ephemeral first.", file=sys.stderr)
        sys.exit(1)

    energy = args.rough_energy_estimate
    if args.table:
        output = view_table(root, energy)
    elif args.month:
        output = view_period(root, "month", "Last 30 Days", energy)
    elif args.week:
        output = view_period(root, "week", "Last 7 Days", energy)
    elif args.day:
        output = view_period(root, "day", "Today", energy)
    else:
        output = view_all(root, energy)

    print(output)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=output.encode(), check=True)
            print("\nCopied to clipboard.", file=sys.stderr)
        except Exception:
            print("\nCould not copy to clipboard.", file=sys.stderr)


if __name__ == "__main__":
    main()
