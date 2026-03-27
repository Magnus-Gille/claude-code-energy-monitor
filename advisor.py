#!/usr/bin/env python3
"""Claude Code Usage Advisor — data-driven recommendations to optimize quota usage.

Analyzes per-session and daily usage data to surface specific, actionable
insights about what's draining your quota and how to reduce it.

Usage:
    python advisor.py              # full analysis (today + recent history)
    python advisor.py --today      # today's sessions only
    python advisor.py --days 7     # last N days
    python advisor.py --json       # machine-readable output
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

CACHE_DIR = Path.home() / ".claude"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"
SESSION_HISTORY_FILE = CACHE_DIR / "statusline_session_history.jsonl"
QUOTA_CACHE = CACHE_DIR / "statusline_quota_cache.json"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_daily_history():
    """Load aggregate daily totals from history.jsonl + today."""
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


def load_today_sessions():
    """Load per-session data from today's daily file."""
    if not DAILY_FILE.exists():
        return []
    daily = json.loads(DAILY_FILE.read_text())
    sessions = []
    for sid, s in daily.get("sessions", {}).items():
        sessions.append({
            "date": daily.get("date", date.today().isoformat()),
            "sid": sid,
            "m": s.get("m", "?"),
            "p": s.get("p", "?"),
            "cws": s.get("cws", 0),
            "cpk": s.get("cpk", 0),
            "$": s.get("$", 0),
            "n": s.get("n", 0),
            "i": s.get("i", 0),
            "o": s.get("o", 0),
            "c": s.get("c", 0),
            "cw": s.get("cw", 0),
            "fs": s.get("fs", 0),
            "ls": s.get("ls", 0),
        })
    return sessions


def load_session_history(since=None):
    """Load per-session records from session history JSONL."""
    sessions = []
    if not SESSION_HISTORY_FILE.exists():
        return sessions
    for line in SESSION_HISTORY_FILE.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if since and rec.get("date", "") < since:
            continue
        sessions.append(rec)
    return sessions


def load_quota():
    """Load cached quota data."""
    if not QUOTA_CACHE.exists():
        return None, None
    try:
        d = json.loads(QUOTA_CACHE.read_text())
        return d.get("q5"), d.get("q7")
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_tok(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def session_total_tokens(s):
    return s.get("i", 0) + s.get("o", 0) + s.get("c", 0) + s.get("cw", 0)


def day_total_tokens(d):
    return (d.get("input", 0) + d.get("output", 0)
            + d.get("cache_read", 0) + d.get("cache_write", 0))


def model_short(model_id):
    if not model_id or model_id == "?":
        return "?"
    if "opus" in model_id:
        return "Opus"
    if "sonnet" in model_id:
        return "Sonnet"
    if "haiku" in model_id:
        return "Haiku"
    return model_id.split("-")[0].title()


# ---------------------------------------------------------------------------
# Advisor rules — each returns a list of (severity, message) tuples
# ---------------------------------------------------------------------------

def rule_spike_detection(daily_history, analysis_days):
    """Compare recent usage to 30-day average."""
    findings = []
    if len(daily_history) < 3:
        return findings

    sorted_days = sorted(daily_history.values(), key=lambda d: d["date"])
    all_totals = [day_total_tokens(d) for d in sorted_days]

    # 30-day average (excluding most recent day)
    avg_window = all_totals[:-1][-30:] if len(all_totals) > 1 else all_totals
    if not avg_window:
        return findings
    avg = sum(avg_window) / len(avg_window)
    if avg == 0:
        return findings

    # Check last N analysis days for spikes
    for d in sorted_days[-analysis_days:]:
        total = day_total_tokens(d)
        if total > 2 * avg:
            ratio = total / avg
            findings.append((
                "warning",
                f"{d['date']} usage ({fmt_tok(total)}) was {ratio:.1f}x "
                f"your {len(avg_window)}-day average ({fmt_tok(int(avg))})"
            ))
    return findings


def rule_long_sessions(sessions):
    """Flag sessions with very high cache_read or context utilization."""
    findings = []
    for s in sessions:
        cache_read = s.get("c", 0)
        cpk = s.get("cpk", 0)
        project = s.get("p", "?")
        if cache_read > 50_000_000:
            findings.append((
                "warning",
                f"Session on {project} read {fmt_tok(cache_read)} cached tokens"
                f" (context {cpk}% full). Long conversations are expensive"
                f" — consider starting fresh after major milestones."
            ))
        elif cpk > 80:
            findings.append((
                "info",
                f"Session on {project} hit {cpk}% context utilization."
                f" Starting fresh earlier reduces per-turn cost."
            ))
    # Cap at top 5 to avoid noise
    findings.sort(key=lambda f: f[0])
    return findings[:5]


def rule_model_selection(sessions):
    """Flag short sessions on expensive models."""
    findings = []
    opus_short = [s for s in sessions
                  if "opus" in s.get("m", "").lower()
                  and s.get("n", 0) > 0 and s.get("n", 0) < 5]
    total_sessions = [s for s in sessions if s.get("n", 0) > 0]

    if len(opus_short) >= 3 and total_sessions:
        pct = len(opus_short) / len(total_sessions) * 100
        if pct >= 15:
            findings.append((
                "warning",
                f"{len(opus_short)} sessions ({pct:.0f}%) used Opus for"
                f" <5 API calls. Sonnet handles quick tasks at lower"
                f" quota cost."
            ))
    return findings


def rule_context_utilization(sessions):
    """Count sessions hitting high context utilization."""
    findings = []
    high_ctx = [s for s in sessions if s.get("cpk", 0) > 80]
    if len(high_ctx) >= 2:
        findings.append((
            "warning",
            f"{len(high_ctx)} sessions hit >80% context utilization."
            f" High-context turns are the most expensive."
            f" Start fresh conversations sooner."
        ))
    return findings


def rule_cache_ratio(daily_history, analysis_days):
    """Check aggregate cache_read:input ratio."""
    findings = []
    sorted_days = sorted(daily_history.values(), key=lambda d: d["date"])
    recent = sorted_days[-analysis_days:]

    total_cr = sum(d.get("cache_read", 0) for d in recent)
    total_in = sum(d.get("input", 0) for d in recent)
    if total_in > 0:
        ratio = total_cr / total_in
        if ratio > 50:
            findings.append((
                "info",
                f"Cache_read:input ratio is {ratio:.0f}:1."
                f" You're re-reading huge cached contexts each turn."
                f" Shorter sessions would reduce this."
            ))
    return findings


def rule_1m_context(sessions):
    """Flag sessions using the 1M context window."""
    findings = []
    big = [s for s in sessions if s.get("cws", 0) > 200_000]
    if big:
        findings.append((
            "info",
            f"{len(big)} session(s) used extended context (>200k)."
            f" These drain quota faster than standard 200k sessions."
        ))
    return findings


def rule_project_breakdown(sessions):
    """Show top projects by total tokens."""
    findings = []
    by_project = {}
    for s in sessions:
        p = s.get("p", "?")
        by_project[p] = by_project.get(p, 0) + session_total_tokens(s)

    if not by_project:
        return findings

    top = sorted(by_project.items(), key=lambda x: -x[1])[:5]
    parts = [f"{p} ({fmt_tok(t)})" for p, t in top if t > 0]
    if parts:
        findings.append((
            "info",
            f"Top projects: {', '.join(parts)}"
        ))
    return findings


def rule_most_expensive_session(sessions):
    """Identify the single most expensive session."""
    findings = []
    active = [s for s in sessions if session_total_tokens(s) > 0]
    if not active:
        return findings

    biggest = max(active, key=session_total_tokens)
    tok = session_total_tokens(biggest)
    findings.append((
        "info",
        f"Biggest session: {biggest.get('p', '?')}"
        f" ({fmt_tok(tok)} tokens, {biggest.get('n', 0)} API calls,"
        f" {model_short(biggest.get('m', '?'))},"
        f" peak context {biggest.get('cpk', 0)}%)"
    ))
    return findings


def rule_quota_projection(q5):
    """Warn if quota burn rate is high."""
    findings = []
    if q5 is None:
        return findings
    if q5 > 60:
        # Rough projection: 5h window, linearly extrapolate
        remaining_pct = 100 - q5
        if q5 > 0:
            hours_left = (remaining_pct / q5) * 5
            findings.append((
                "alert",
                f"5-hour quota at {q5:.0f}%."
                f" At current pace, you'll hit the limit in"
                f" ~{hours_left:.1f}h. Consider pausing or switching"
                f" to Sonnet."
            ))
    elif q5 > 40:
        findings.append((
            "info",
            f"5-hour quota at {q5:.0f}% — moderate usage pace."
        ))
    return findings


def rule_model_distribution(sessions):
    """Show model usage distribution."""
    findings = []
    by_model = {}
    for s in sessions:
        m = model_short(s.get("m", "?"))
        by_model[m] = by_model.get(m, 0) + session_total_tokens(s)

    if not by_model:
        return findings

    total = sum(by_model.values())
    if total == 0:
        return findings

    parts = []
    for m, t in sorted(by_model.items(), key=lambda x: -x[1]):
        pct = t / total * 100
        parts.append(f"{m}: {fmt_tok(t)} ({pct:.0f}%)")

    findings.append((
        "info",
        f"Model mix: {', '.join(parts)}"
    ))
    return findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

SEVERITY_ICON = {
    "alert": "!!",
    "warning": " !",
    "info": "  ",
}

SEVERITY_ORDER = {"alert": 0, "warning": 1, "info": 2}


def run_analysis(days_back=7, today_only=False):
    """Run all advisor rules and return sorted findings."""
    daily_history = load_daily_history()
    q5, q7 = load_quota()

    # Determine analysis window
    if today_only:
        sessions = load_today_sessions()
        analysis_days = 1
    else:
        since = (date.today() - timedelta(days=days_back)).isoformat()
        sessions = load_session_history(since=since) + load_today_sessions()
        analysis_days = days_back

    findings = []
    findings.extend(rule_spike_detection(daily_history, analysis_days))
    findings.extend(rule_long_sessions(sessions))
    findings.extend(rule_model_selection(sessions))
    findings.extend(rule_context_utilization(sessions))
    findings.extend(rule_cache_ratio(daily_history, analysis_days))
    findings.extend(rule_1m_context(sessions))
    findings.extend(rule_quota_projection(q5))
    findings.extend(rule_model_distribution(sessions))
    findings.extend(rule_project_breakdown(sessions))
    findings.extend(rule_most_expensive_session(sessions))

    # Sort: alerts first, then warnings, then info
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f[0], 99))
    return findings, len(sessions), analysis_days


def format_report(findings, session_count, analysis_days):
    """Format findings as terminal output."""
    lines = []
    lines.append(f"Claude Code Usage Advisor")
    if analysis_days == 1:
        lines.append(f"  Today · {session_count} sessions analyzed")
    else:
        lines.append(
            f"  Last {analysis_days} days · {session_count} sessions analyzed")
    lines.append("")

    if not findings:
        lines.append("  No issues detected. Usage looks normal.")
        return "\n".join(lines)

    alerts = [f for f in findings if f[0] == "alert"]
    warnings = [f for f in findings if f[0] == "warning"]
    infos = [f for f in findings if f[0] == "info"]

    if alerts:
        lines.append("  ALERTS")
        for _, msg in alerts:
            lines.append(f"  !! {msg}")
        lines.append("")

    if warnings:
        lines.append("  WARNINGS")
        for _, msg in warnings:
            lines.append(f"   ! {msg}")
        lines.append("")

    if infos:
        lines.append("  INFO")
        for _, msg in infos:
            lines.append(f"     {msg}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Usage Advisor — data-driven quota optimization")
    parser.add_argument("--today", action="store_true",
                        help="analyze today's sessions only")
    parser.add_argument("--days", type=int, default=7,
                        help="number of days to analyze (default: 7)")
    parser.add_argument("--json", action="store_true",
                        help="output findings as JSON")
    args = parser.parse_args()

    findings, session_count, analysis_days = run_analysis(
        days_back=args.days, today_only=args.today)

    if args.json:
        output = [{"severity": s, "message": m} for s, m in findings]
        print(json.dumps(output, indent=2))
    else:
        print(format_report(findings, session_count, analysis_days))


if __name__ == "__main__":
    main()
