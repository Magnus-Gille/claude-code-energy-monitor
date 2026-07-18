#!/usr/bin/env python3
"""Pi Step Counter — shareable usage summaries from Pi harness sessions."""

import argparse
import subprocess
import sys
from pathlib import Path

from codex_stepcount import energy_comparison, fmt_energy
from codex_status import fmt_tok
from pi_status import SESSIONS_DIR, build_payload


def period_rows(payload):
    return [("Today", payload["day"]), ("Week", payload["week"]), ("Month", payload["month"])]


def view_period(payload, period_key: str, title: str, energy: bool) -> str:
    period = payload[period_key]
    output = (
        f"⚡ Pi · {title}\n{fmt_tok(int(period['total_tokens']))} tokens · "
        f"{int(period['sessions'])} sessions"
    )
    if energy:
        output += f"\n{energy_comparison(float(period['energy_mwh']) / 1000)}"
    return output


def view_all(payload, energy: bool) -> str:
    rows = period_rows(payload)
    token_strings = [fmt_tok(int(period["total_tokens"])) for _, period in rows]
    token_width = max(len(value) for value in token_strings)
    session_width = max(len(str(int(period["sessions"]))) for _, period in rows)
    lines = ["⚡ Pi"]
    energy_strings = [fmt_energy(float(period["energy_mwh"]) / 1000) for _, period in rows]
    energy_width = max(len(value) for value in energy_strings)
    for (label, period), tokens, energy_string in zip(rows, token_strings, energy_strings):
        line = (
            f"   {label:<5} {tokens:>{token_width}} tokens · "
            f"{int(period['sessions']):>{session_width}} sessions"
        )
        if energy:
            line += f" · {energy_string:>{energy_width}}"
        lines.append(line)
    return "\n".join(lines)


def view_table(payload, energy: bool) -> str:
    rows = period_rows(payload)
    token_values = [int(period["total_tokens"]) for _, period in rows]
    token_strings = [fmt_tok(value) for value in token_values]
    maximum = max(token_values) or 1
    token_width = max(max(len(value) for value in token_strings), 6)
    session_width = max(max(len(str(int(period["sessions"]))) for _, period in rows), 4)
    bar_width = 10

    def bar(value: int) -> str:
        filled = round(value / maximum * bar_width)
        if value > 0 and filled == 0:
            filled = 1
        return "█" * filled + "░" * (bar_width - filled)

    lines = ["⚡ Pi"]
    lines.append(f"   ┌{'─' * 7}┬{'─' * (token_width + 2)}┬{'─' * (session_width + 2)}┬{'─' * (bar_width + 2)}┐")
    lines.append(f"   │       │ {'tokens':>{token_width}} │ {'sess':>{session_width}} │ {' ' * bar_width} │")
    lines.append(f"   ├{'─' * 7}┼{'─' * (token_width + 2)}┼{'─' * (session_width + 2)}┼{'─' * (bar_width + 2)}┤")
    for (label, period), tokens, value in zip(rows, token_strings, token_values):
        lines.append(
            f"   │ {label:<5} │ {tokens:>{token_width}} │ "
            f"{int(period['sessions']):>{session_width}} │ {bar(value)} │"
        )
    lines.append(f"   └{'─' * 7}┴{'─' * (token_width + 2)}┴{'─' * (session_width + 2)}┴{'─' * (bar_width + 2)}┘")
    if energy:
        lines.append(f"   {energy_comparison(float(rows[-1][1]['energy_mwh']) / 1000)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="⚡ Pi Step Counter — shareable usage summaries")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-d", "--day", action="store_true", help="Today only")
    group.add_argument("-w", "--week", action="store_true", help="Last 7 days only")
    group.add_argument("-m", "--month", action="store_true", help="Last 30 days only")
    group.add_argument("-t", "--table", action="store_true", help="Show all periods as an ASCII table")
    parser.add_argument("--rough-energy-estimate", action="store_true", help="Include order-of-magnitude energy guess (±3x)")
    parser.add_argument("--copy", action="store_true", help="Copy output to clipboard")
    parser.add_argument("--root", type=Path, default=SESSIONS_DIR, help="Pi sessions root (default: ~/.pi/agent/sessions)")
    args = parser.parse_args()

    root = args.root.expanduser()
    payload = build_payload(root, None)
    if all(int(payload[key]["total_tokens"]) == 0 for key in ("day", "week", "month")):
        print("No Pi session usage found. Run Pi without --no-session first.", file=sys.stderr)
        raise SystemExit(1)

    energy = args.rough_energy_estimate
    if args.table:
        output = view_table(payload, energy)
    elif args.month:
        output = view_period(payload, "month", "Last 30 Days", energy)
    elif args.week:
        output = view_period(payload, "week", "Last 7 Days", energy)
    elif args.day:
        output = view_period(payload, "day", "Today", energy)
    else:
        output = view_all(payload, energy)
    print(output)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=output.encode(), check=True)
            print("\nCopied to clipboard.", file=sys.stderr)
        except Exception:
            print("\nCould not copy to clipboard.", file=sys.stderr)


if __name__ == "__main__":
    main()
