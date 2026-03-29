#!/usr/bin/env python3
"""Claude Code Usage Advisor — data-driven recommendations to optimize quota usage.

Analyzes per-session and daily usage data to surface specific, actionable
insights about what's draining your quota and how to reduce it.

Usage:
    python advisor.py              # full analysis (today + recent history)
    python advisor.py --today      # today's sessions only
    python advisor.py --days 7     # last N days
    python advisor.py --breakdown  # per-project/model cost breakdown
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
PI_JOURNAL_FILE = CACHE_DIR / "pi_journal.jsonl"
PI_ROLLUP_FILE = CACHE_DIR / "pi_daily_rollup.jsonl"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_daily_history(include_pi=True):
    """Load aggregate daily totals from history.jsonl + today + Pi."""
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
    if include_pi:
        _merge_pi_rollup(days)
    return days


def load_today_sessions():
    """Load per-session data from today's daily file.

    Includes daily delta fields (di/do/dc/dcw) when available.
    These represent today's usage for each session, not cumulative lifetime.
    """
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
            # Daily deltas (today's contribution only)
            "di": s.get("di", 0),
            "do": s.get("do", 0),
            "dc": s.get("dc", 0),
            "dcw": s.get("dcw", 0),
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


def load_pi_journal(since=None):
    """Load Pi invocation journal and translate to advisor session schema."""
    sessions = []
    if not PI_JOURNAL_FILE.exists():
        return sessions
    for line in PI_JOURNAL_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        date_str = entry.get("ts", "")[:10]
        if since and date_str < since:
            continue
        # Translate Pi journal entry to advisor session schema.
        # Pi journal entries are per-invocation, so the token values
        # ARE the daily deltas (one entry = one invocation's full usage).
        i = entry.get("input", 0)
        o = entry.get("output", 0)
        c = entry.get("cache_read", 0)
        cw = entry.get("cache_write", 0)
        sessions.append({
            "date": date_str,
            "sid": entry.get("sid", "?"),
            "m": entry.get("model", "?"),
            "p": entry.get("project", "?"),
            "cws": 0,
            "cpk": 0,
            "$": 0,
            "n": entry.get("turns", 1),
            "i": i, "o": o, "c": c, "cw": cw,
            "di": i, "do": o, "dc": c, "dcw": cw,
            "fs": 0, "ls": 0,
            "machine": entry.get("machine", "pi"),
        })
    return sessions


def _merge_pi_rollup(days):
    """Merge Pi daily rollup into existing daily history (additive)."""
    if not PI_ROLLUP_FILE.exists():
        return
    for line in PI_ROLLUP_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        date_str = d["date"]
        if date_str in days:
            days[date_str]["input"] += d.get("input", 0)
            days[date_str]["output"] += d.get("output", 0)
            days[date_str]["cache_read"] += d.get("cache_read", 0)
            days[date_str]["cache_write"] += d.get("cache_write", 0)
            days[date_str]["sessions"] += d.get("sessions", 0)
        else:
            days[date_str] = {
                "date": date_str,
                "input": d.get("input", 0),
                "output": d.get("output", 0),
                "cache_read": d.get("cache_read", 0),
                "cache_write": d.get("cache_write", 0),
                "sessions": d.get("sessions", 0),
            }


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


def session_daily_tokens(s):
    """Today's token usage for a session (uses daily deltas if available)."""
    di = s.get("di", 0)
    do = s.get("do", 0)
    dc = s.get("dc", 0)
    dcw = s.get("dcw", 0)
    if di + do + dc + dcw > 0:
        return di + do + dc + dcw
    # Fallback for legacy data without daily deltas
    return session_total_tokens(s)


def quota_cost_weight(s):
    """Estimate relative quota cost for a session.

    Anthropic's quota system weights output tokens (especially thinking)
    much more heavily than input/cache. Exact weights are unknown, so we
    use approximate API pricing ratios as a proxy:
      - Output: 5x input (includes thinking tokens)
      - Cache read: 0.1x input
      - Cache write: 1.25x input
    """
    di = s.get("di", s.get("i", 0))
    do = s.get("do", s.get("o", 0))
    dc = s.get("dc", s.get("c", 0))
    dcw = s.get("dcw", s.get("cw", 0))
    return di + do * 5 + dc * 0.1 + dcw * 1.25


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
# Quota breakdown — "where did my tokens go?"
# ---------------------------------------------------------------------------

def _has_daily_deltas(sessions):
    """Check if any sessions have daily delta fields."""
    return any(s.get("di", 0) + s.get("do", 0) + s.get("dc", 0) + s.get("dcw", 0) > 0
               for s in sessions)


def _session_deltas(s):
    """Extract daily deltas, falling back to cumulative for legacy data."""
    has_deltas = (s.get("di", 0) + s.get("do", 0)
                  + s.get("dc", 0) + s.get("dcw", 0)) > 0
    if has_deltas:
        return s["di"], s["do"], s["dc"], s["dcw"]
    return s.get("i", 0), s.get("o", 0), s.get("c", 0), s.get("cw", 0)


def _breakdown_by_project(sessions):
    """Aggregate today's usage by project."""
    by_project = {}
    for s in sessions:
        p = s.get("p", "?")
        di, do, dc, dcw = _session_deltas(s)
        if di + do + dc + dcw == 0:
            continue
        if p not in by_project:
            by_project[p] = {"i": 0, "o": 0, "c": 0, "cw": 0,
                             "sessions": 0, "calls": 0, "models": set()}
        by_project[p]["i"] += di
        by_project[p]["o"] += do
        by_project[p]["c"] += dc
        by_project[p]["cw"] += dcw
        by_project[p]["sessions"] += 1
        by_project[p]["calls"] += s.get("n", 0)
        m = model_short(s.get("m", "?"))
        if m != "?":
            by_project[p]["models"].add(m)
    return by_project


def _breakdown_by_model(sessions):
    """Aggregate today's usage by model."""
    by_model = {}
    for s in sessions:
        m = model_short(s.get("m", "?"))
        di, do, dc, dcw = _session_deltas(s)
        if di + do + dc + dcw == 0:
            continue
        if m not in by_model:
            by_model[m] = {"i": 0, "o": 0, "c": 0, "cw": 0,
                           "sessions": 0, "calls": 0}
        by_model[m]["i"] += di
        by_model[m]["o"] += do
        by_model[m]["c"] += dc
        by_model[m]["cw"] += dcw
        by_model[m]["sessions"] += 1
        by_model[m]["calls"] += s.get("n", 0)
    return by_model


def _top_sessions(sessions, n=10):
    """Return top N sessions by quota cost weight."""
    active = [s for s in sessions
              if _session_deltas(s) != (0, 0, 0, 0)]
    return sorted(active, key=quota_cost_weight, reverse=True)[:n]


def _cost_weight(i, o, c, cw):
    """Compute cost weight from token counts."""
    return i + o * 5 + c * 0.1 + cw * 1.25


def format_breakdown(sessions, include_pi=True):
    """Format per-project/model breakdown as terminal output."""
    lines = []
    q5, q7 = load_quota()

    # Header
    lines.append("Quota Breakdown")
    if q5 is not None:
        parts = [f"5h: {q5:.0f}%"]
        if q7 is not None:
            parts.append(f"7d: {q7:.0f}%")
        lines.append(f"  Current quota: {' | '.join(parts)}")

    # Daily totals from top-level (accurate)
    daily_history = load_daily_history(include_pi=include_pi)
    today_str = date.today().isoformat()
    today_agg = daily_history.get(today_str, {})
    d_i = today_agg.get("input", 0)
    d_o = today_agg.get("output", 0)
    d_c = today_agg.get("cache_read", 0)
    d_cw = today_agg.get("cache_write", 0)

    if d_i + d_o + d_c + d_cw > 0:
        lines.append(f"  Today: input={fmt_tok(d_i)}  output={fmt_tok(d_o)}"
                     f"  cache_read={fmt_tok(d_c)}  cache_write={fmt_tok(d_cw)}")
    lines.append("")

    has_deltas = _has_daily_deltas(sessions)
    if not has_deltas:
        lines.append("  NOTE: Per-session daily deltas not yet available.")
        lines.append("  Showing cumulative session totals (may overcount for")
        lines.append("  sessions spanning multiple days). Deltas will populate")
        lines.append("  as the statusline processes new API calls.")
        lines.append("")

    # Filter to active sessions
    active = [s for s in sessions
              if _session_deltas(s) != (0, 0, 0, 0)]
    if not active:
        lines.append("  No active sessions found.")
        return "\n".join(lines)

    # ── By Project ──
    by_project = _breakdown_by_project(sessions)
    if by_project:
        sorted_projects = sorted(
            by_project.items(),
            key=lambda x: _cost_weight(x[1]["i"], x[1]["o"], x[1]["c"], x[1]["cw"]),
            reverse=True)

        total_cw = sum(_cost_weight(v["i"], v["o"], v["c"], v["cw"])
                       for _, v in sorted_projects)

        lines.append("  By Project")
        pw = max(len(p) for p, _ in sorted_projects[:10])
        pw = min(max(pw, 7), 25)
        lines.append(f"  {'Project':<{pw}}  {'Output':>8}  {'CacheRd':>8}  "
                     f"{'Sess':>4}  {'Calls':>5}  {'Share':>5}  {'Models'}")
        lines.append(f"  {'-' * pw}  {'-' * 8}  {'-' * 8}  "
                     f"{'-' * 4}  {'-' * 5}  {'-' * 5}  {'-' * 10}")
        for p, v in sorted_projects[:10]:
            cw = _cost_weight(v["i"], v["o"], v["c"], v["cw"])
            pct = cw / total_cw * 100 if total_cw > 0 else 0
            models_str = ",".join(sorted(v["models"])) if v["models"] else "?"
            lines.append(
                f"  {p[:pw]:<{pw}}  {fmt_tok(v['o']):>8}  {fmt_tok(v['c']):>8}  "
                f"{v['sessions']:>4}  {v['calls']:>5}  {pct:>4.0f}%  {models_str}")
        lines.append("")

    # ── By Model ──
    by_model = _breakdown_by_model(sessions)
    if by_model:
        sorted_models = sorted(
            by_model.items(),
            key=lambda x: _cost_weight(x[1]["i"], x[1]["o"], x[1]["c"], x[1]["cw"]),
            reverse=True)

        total_cw = sum(_cost_weight(v["i"], v["o"], v["c"], v["cw"])
                       for _, v in sorted_models)

        lines.append("  By Model")
        lines.append(f"  {'Model':<8}  {'Input':>8}  {'Output':>8}  {'CacheRd':>8}  "
                     f"{'Sess':>4}  {'Calls':>5}  {'Share':>5}")
        lines.append(f"  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 8}  "
                     f"{'-' * 4}  {'-' * 5}  {'-' * 5}")
        for m, v in sorted_models:
            cw = _cost_weight(v["i"], v["o"], v["c"], v["cw"])
            pct = cw / total_cw * 100 if total_cw > 0 else 0
            lines.append(
                f"  {m:<8}  {fmt_tok(v['i']):>8}  {fmt_tok(v['o']):>8}  "
                f"{fmt_tok(v['c']):>8}  {v['sessions']:>4}  {v['calls']:>5}  "
                f"{pct:>4.0f}%")
        lines.append("")

    # ── Top Sessions ──
    top = _top_sessions(sessions, n=10)
    if top:
        lines.append("  Top Sessions (by estimated quota impact)")
        lines.append(f"  {'SID':>8}  {'Project':>20}  {'Model':>6}  "
                     f"{'Output':>8}  {'CacheRd':>8}  {'Calls':>5}  {'Ctx%':>4}")
        lines.append(f"  {'-' * 8}  {'-' * 20}  {'-' * 6}  "
                     f"{'-' * 8}  {'-' * 8}  {'-' * 5}  {'-' * 4}")
        for s in top:
            di, do, dc, dcw = _session_deltas(s)
            lines.append(
                f"  {s['sid'][:8]:>8}  {s.get('p', '?')[:20]:>20}  "
                f"{model_short(s.get('m', '?')):>6}  "
                f"{fmt_tok(do):>8}  {fmt_tok(dc):>8}  "
                f"{s.get('n', 0):>5}  {s.get('cpk', 0):>3.0f}%")
        lines.append("")

    # ── Quota Driver Analysis ──
    if d_o > 0:
        output_share = (d_o * 5) / _cost_weight(d_i, d_o, d_c, d_cw) * 100
        lines.append("  Quota Driver Analysis")
        lines.append(f"  Output tokens ({fmt_tok(d_o)}) account for ~{output_share:.0f}%"
                     " of estimated quota cost.")
        lines.append("  Output includes thinking tokens (not separable),")
        lines.append("  which dominate Opus quota consumption.")
        if output_share > 60:
            lines.append("  TIP: Shorter conversations or Sonnet for simple tasks")
            lines.append("  would reduce output token generation significantly.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

SEVERITY_ICON = {
    "alert": "!!",
    "warning": " !",
    "info": "  ",
}

SEVERITY_ORDER = {"alert": 0, "warning": 1, "info": 2}


def run_analysis(days_back=7, today_only=False, include_pi=True):
    """Run all advisor rules and return sorted findings."""
    daily_history = load_daily_history(include_pi=include_pi)
    q5, q7 = load_quota()

    # Determine analysis window
    if today_only:
        sessions = load_today_sessions()
        if include_pi:
            sessions += load_pi_journal(
                since=date.today().isoformat())
        analysis_days = 1
    else:
        since = (date.today() - timedelta(days=days_back)).isoformat()
        sessions = load_session_history(since=since) + load_today_sessions()
        if include_pi:
            sessions += load_pi_journal(since=since)
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
    parser.add_argument("--breakdown", action="store_true",
                        help="show per-project/model quota breakdown")
    parser.add_argument("--no-pi", action="store_true",
                        help="exclude Pi/headless data from analysis")
    parser.add_argument("--json", action="store_true",
                        help="output findings as JSON")
    args = parser.parse_args()
    include_pi = not args.no_pi

    if args.breakdown:
        sessions = load_today_sessions()
        if include_pi:
            sessions += load_pi_journal(
                since=date.today().isoformat())
        print(format_breakdown(sessions, include_pi=include_pi))
        return

    findings, session_count, analysis_days = run_analysis(
        days_back=args.days, today_only=args.today,
        include_pi=include_pi)

    if args.json:
        output = [{"severity": s, "message": m} for s, m in findings]
        print(json.dumps(output, indent=2))
    else:
        print(format_report(findings, session_count, analysis_days))


if __name__ == "__main__":
    main()
