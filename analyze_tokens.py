#!/usr/bin/env python3
"""Analyze statusline debug logs to validate token-counting semantics.

Answers two key questions:
  1. Does total_input_tokens include cache_creation_input_tokens? (double-counting?)
  2. Does total_output_tokens include thinking tokens?

Usage:
  1. Enable debug logging:  export ENERGY_DEBUG=1
  2. Use Claude Code normally for a session (a few exchanges is enough)
  3. Run:  python3 analyze_tokens.py

The script reads ~/.claude/statusline_debug.jsonl and prints a report.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

DEBUG_FILE = Path.home() / ".claude" / "statusline_debug.jsonl"


def load_entries(path):
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def analyze(entries):
    # Group by session
    by_session = defaultdict(list)
    for e in entries:
        raw = e.get("raw", {})
        sid = raw.get("session_id", "unknown")
        by_session[sid].append(e)

    # Sort each session by timestamp
    for sid in by_session:
        by_session[sid].sort(key=lambda e: e.get("ts", 0))

    all_calls = []  # collected across sessions for summary stats

    for sid, session_entries in sorted(by_session.items()):
        print(f"\n{'='*72}")
        print(f"Session: {sid[:20]}...")
        print(f"  Entries: {len(session_entries)}")

        prev_total_in = 0
        prev_total_out = 0
        call_num = 0
        calls = []
        last_finalized_cu = {}  # cu values from the last entry before a new call

        for idx, e in enumerate(session_entries):
            raw = e.get("raw", {})
            ctx = raw.get("context_window", {})
            cu = ctx.get("current_usage") or {}

            total_in = ctx.get("total_input_tokens", 0)
            total_out = ctx.get("total_output_tokens", 0)

            # Detect new API call: total_input increased
            if total_in > prev_total_in:
                call_num += 1
                delta_in = total_in - prev_total_in
                delta_out = total_out - prev_total_out

                call = {
                    "num": call_num,
                    "total_in": total_in,
                    "total_out": total_out,
                    "delta_in": delta_in,
                    "delta_out": delta_out,
                    "cu_at_start": dict(cu),  # cu when new call starts (may be placeholder)
                    "cu_finalized_prev": dict(last_finalized_cu),  # cu at end of previous call
                    "cu": cu,
                    "cu_input": cu.get("input_tokens", None),
                    "cu_output": cu.get("output_tokens", None),
                    "cu_cache_read": cu.get("cache_read_input_tokens", 0),
                    "cu_cache_create": cu.get("cache_creation_input_tokens", 0),
                }
                calls.append(call)
                all_calls.append(call)

                prev_total_in = total_in
                prev_total_out = total_out

            # Always update last_finalized_cu (the last one before next jump is "finalized")
            last_finalized_cu = cu

        if not calls:
            print("  No API calls detected.")
            continue

        print(f"  API calls detected: {len(calls)}")
        print()

        # Show all available fields in current_usage (first call as sample)
        sample_cu = calls[0]["cu"]
        print(f"  current_usage fields: {sorted(sample_cu.keys())}")
        print()

        # Per-call analysis
        print(f"  {'Call':>4} | {'delta_total_in':>14} | {'cu.input':>10} | {'cu.cache_cr':>11} | "
              f"{'cu.input+cc':>12} | {'delta_total_out':>15} | {'cu.output':>10} | {'out_ratio':>9}")
        print(f"  {'-'*4}-+-{'-'*14}-+-{'-'*10}-+-{'-'*11}-+-{'-'*12}-+-{'-'*15}-+-{'-'*10}-+-{'-'*9}")

        for c in calls:
            cu_in = c["cu_input"]
            cu_out = c["cu_output"]
            cu_cc = c["cu_cache_create"]
            cu_in_plus_cc = (cu_in + cu_cc) if cu_in is not None else None

            # Output ratio: delta_total_out / cu.output (>1 means thinking included)
            out_ratio = ""
            if cu_out and cu_out > 0 and c["delta_out"] > 0:
                out_ratio = f"{c['delta_out'] / cu_out:.1f}x"

            cu_in_s = str(cu_in) if cu_in is not None else "n/a"
            cu_out_s = str(cu_out) if cu_out is not None else "n/a"
            cu_in_cc_s = str(cu_in_plus_cc) if cu_in_plus_cc is not None else "n/a"

            print(f"  {c['num']:>4} | {c['delta_in']:>14,} | {cu_in_s:>10} | {cu_cc:>11,} | "
                  f"{cu_in_cc_s:>12} | {c['delta_out']:>15,} | {cu_out_s:>10} | {out_ratio:>9}")

        # Show finalized previous-call cu for comparison (helps spot placeholders)
        if len(calls) > 1:
            print(f"\n  Finalized cu from previous call (helps distinguish placeholders):")
            for c in calls[1:]:
                prev_cu = c.get("cu_finalized_prev", {})
                print(f"    Call {c['num']}: prev_cu.input={prev_cu.get('input_tokens', '?')}, "
                      f"prev_cu.output={prev_cu.get('output_tokens', '?')}, "
                      f"prev_cu.cache_create={prev_cu.get('cache_creation_input_tokens', '?')}, "
                      f"prev_cu.cache_read={prev_cu.get('cache_read_input_tokens', '?')}")

    # Summary analysis
    print(f"\n{'='*72}")
    print("SUMMARY ANALYSIS")
    print(f"{'='*72}")
    print(f"Total API calls analyzed: {len(all_calls)}")

    if not all_calls:
        print("No data to analyze. Run with ENERGY_DEBUG=1 first.")
        return

    # Q1: Does total_input_tokens include cache_creation?
    # Filter to calls where cu.input > 1 (exclude placeholder values) and
    # delta is not the first-of-session catch-up (exclude call_num == 1 with huge delta).
    usable_q1 = [c for c in all_calls
                 if c["cu_input"] is not None
                 and not (c["num"] == 1 and c["delta_in"] > 10000)]

    if usable_q1:
        print(f"\nQ1: Does total_input_tokens include cache_creation_input_tokens?")
        print(f"    (Usable calls: {len(usable_q1)}, excluded {len(all_calls) - len(usable_q1)} "
              f"first-of-session / placeholder entries)")

        match_fresh = 0  # delta ≈ cu.input (no double-counting)
        match_plus_cc = 0  # delta ≈ cu.input + cu.cache_creation (double-counting)
        match_neither = 0
        details = []

        for c in usable_q1:
            cu_in = c["cu_input"]
            cu_cc = c["cu_cache_create"]
            delta = c["delta_in"]

            # Allow 5% tolerance for rounding, min 10 tokens
            tol = max(delta * 0.05, 10)

            if abs(delta - cu_in) <= tol:
                match_fresh += 1
                details.append((c, "fresh_only"))
            elif abs(delta - (cu_in + cu_cc)) <= tol:
                match_plus_cc += 1
                details.append((c, "includes_cc"))
            else:
                match_neither += 1
                details.append((c, "neither"))

        print(f"    delta ≈ cu.input (fresh only):      {match_fresh:>4} calls  (no double-counting)")
        print(f"    delta ≈ cu.input + cu.cache_create:  {match_plus_cc:>4} calls  (would mean double-counting)")
        print(f"    neither match:                       {match_neither:>4} calls")

        if match_neither > 0:
            print(f"\n    'Neither' calls (for investigation):")
            for c, label in details:
                if label == "neither":
                    print(f"      Call {c['num']}: delta={c['delta_in']:,}  "
                          f"cu.input={c['cu_input']}  cu.cc={c['cu_cache_create']:,}  "
                          f"cu.input+cc={c['cu_input'] + c['cu_cache_create']:,}")

        if match_fresh > match_plus_cc:
            print(f"\n    CONCLUSION: total_input_tokens EXCLUDES cache_creation.")
            print(f"    => No double-counting. Current energy formula is correct.")
        elif match_plus_cc > match_fresh:
            print(f"\n    CONCLUSION: total_input_tokens INCLUDES cache_creation.")
            print(f"    => DOUBLE-COUNTING detected! Energy formula needs fix.")
        else:
            print(f"\n    INCONCLUSIVE: Need more data.")
    else:
        print(f"\nQ1: Not enough usable data yet. Keep ENERGY_DEBUG=1 and generate more calls.")

    # Q2: Does total_output_tokens include thinking?
    # Filter to finalized calls: cu.output > 10 (exclude placeholders like 1 or 8)
    # and delta_out > 10
    finalized_output = [c for c in all_calls
                        if c["cu_output"] is not None
                        and c["cu_output"] > 10
                        and c["delta_out"] > 10]

    print(f"\nQ2: Does total_output_tokens include thinking tokens?")
    if finalized_output:
        print(f"    (Finalized calls with cu.output > 10: {len(finalized_output)})")

        ratios = [c["delta_out"] / c["cu_output"] for c in finalized_output]
        avg_ratio = sum(ratios) / len(ratios)
        min_ratio = min(ratios)
        max_ratio = max(ratios)

        print(f"    delta_total_out / cu.output ratio (finalized calls only):")
        print(f"      min:  {min_ratio:.2f}x")
        print(f"      avg:  {avg_ratio:.2f}x")
        print(f"      max:  {max_ratio:.2f}x")

        if 0.95 <= avg_ratio <= 1.05:
            print(f"\n    Ratios ≈ 1.0x: total_output accumulates cu.output exactly.")
            print(f"    This means BOTH include (or both exclude) thinking tokens.")
            print(f"    Since Anthropic API's usage.output_tokens includes thinking,")
            print(f"    total_output_tokens almost certainly INCLUDES thinking.")
            print(f"    (Confirmed by FINDINGS.md: 3x ratio vs JSONL message.output_tokens)")
        elif avg_ratio > 1.5:
            print(f"\n    total_output_tokens grows faster than cu.output.")
            print(f"    Extra tokens likely thinking (not in cu.output).")
        else:
            print(f"\n    Mild discrepancy — may be streaming artifacts.")
    else:
        print(f"    Not enough finalized output data yet.")

    # Show all unique keys found in current_usage across all calls
    all_cu_keys = set()
    for c in all_calls:
        all_cu_keys.update(c["cu"].keys())
    print(f"\nAll current_usage fields observed: {sorted(all_cu_keys)}")


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEBUG_FILE
    if not path.exists():
        print(f"No debug log found at {path}")
        print(f"Enable debug logging:  export ENERGY_DEBUG=1")
        print(f"Then use Claude Code for a few exchanges and run this again.")
        sys.exit(1)

    entries = load_entries(path)
    if not entries:
        print(f"Debug log at {path} is empty.")
        sys.exit(1)

    print(f"Loaded {len(entries)} entries from {path}")
    analyze(entries)


if __name__ == "__main__":
    main()
