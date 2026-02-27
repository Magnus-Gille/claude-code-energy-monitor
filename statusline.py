#!/usr/bin/env python3
"""Claude Code statusline: model, context, quota, tokens, energy estimate, history.

Displays order-of-magnitude energy estimates (e.g. ~5kWh) for daily,
weekly, and monthly usage. No one outside Anthropic knows the actual
energy per token — these are derived estimates, not measurements.

Mid estimates (mWh per 1k tokens), hybrid physics + pricing derivation:
  - Fresh input:   390   (Epoch AI long-context anchor, unchanged from Couch)
  - Output:      1,400   (cross-checks cluster 600-1800; reduced from 1950)
  - Cache read:     15   (~26x discount vs input; physics-derived, see below)
  - Cache write:   490   (prefill + write overhead, ~1.25x fresh input)

The output and cache read constants were revised via adversarial debate
(Claude vs Codex, Feb 2026; see debate/energy-constants-summary.md):
  - Output reduced from 1950→1400: pricing ratio (5:1) overstated decode
    cost vs FLOP-based estimates and AI Energy Score benchmarks.
  - Cache read reduced from 39→15: pricing ratio (10:1) reflected business
    strategy and storage amortization, not compute energy. Physics shows
    cache reads skip all prefill computation (just KV cache loading from
    memory). True discount is 26-1000x vs fresh input; 26x is conservative.

Displayed as order-of-magnitude (snaps to 1/2/5 per decade) because the
real uncertainty is at least ±3x in each direction.

Primary sources:
  Couch (2026)    https://www.simonpcouch.com/blog/2026-01-20-cc-impact/
  Epoch AI (2025) https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use
  Google (2025)   https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference
  AI Energy Score https://huggingface.co/spaces/AIEnergyScore/Leaderboard

What this does NOT capture:
  - Training energy, embodied energy, networking
  - Reasoning/extended thinking overhead
  - Geographic carbon intensity variation
  - Actual hardware, batch sizes, or optimizations used by Anthropic

NOTE: Quota fetching uses an UNDOCUMENTED Anthropic beta API endpoint
(/api/oauth/usage with anthropic-beta: oauth-2025-04-20). This is
subject to breaking changes or removal without notice.
"""

import fcntl
import json
import os
import sys
import subprocess
import time
from pathlib import Path
from datetime import date, timedelta

CACHE_DIR = Path.home() / ".claude"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"
QUOTA_CACHE = CACHE_DIR / "statusline_quota_cache.json"
QUOTA_TTL = 300  # seconds between API calls
DEBUG_FILE = CACHE_DIR / "statusline_debug.jsonl"
DEBUG = os.environ.get("ENERGY_DEBUG", "") == "1"

# Energy: mWh per 1k tokens — mid estimates
# Hybrid constants from Couch (2026) base + physics-derived cache/output
# adjustments via adversarial debate (see debate/energy-constants-summary.md).
# Fresh input: Epoch AI long-context anchor (unchanged)
# Output: reduced from 1950→1400 (cross-checks cluster 600-1800)
# Cache read: reduced from 39→15 (~26x discount vs input; pricing 10x was too conservative)
# Cache write: unchanged (prefill + write overhead)
E_IN = 390      # fresh input (long-context workload)
E_OUT = 1400    # output (decode)
E_CACHE = 15    # cached input (cache read)
E_CW = 490      # cache creation (write)


def load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save(path, obj):
    """Atomic write: temp file -> fsync -> rename. Owner-only permissions."""
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def get_token():
    """Get OAuth token from macOS Keychain, trying current user first."""
    user = os.environ.get("USER", "")
    attempts = []
    if user:
        attempts.append(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", user, "-w"]
        )
    attempts.append(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
    )

    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                continue
            raw = r.stdout.strip()
            try:
                creds = json.loads(raw)
                oauth = creds.get("claudeAiOauth", {})
                if isinstance(oauth, dict) and "accessToken" in oauth:
                    exp = oauth.get("expiresAt", 0)
                    if isinstance(exp, (int, float)) and exp > 0:
                        if exp / 1000 < time.time():
                            continue  # expired, try next
                    return oauth["accessToken"]
                if "accessToken" in creds:
                    return creds["accessToken"]
                if "access_token" in creds:
                    return creds["access_token"]
            except json.JSONDecodeError:
                return raw
        except Exception:
            continue
    return None


def fetch_quota():
    """Fetch quota from Anthropic OAuth API (UNDOCUMENTED BETA endpoint)."""
    cache = load(QUOTA_CACHE)
    now = time.time()
    if cache and now - cache.get("ts", 0) < QUOTA_TTL:
        return cache.get("q5"), cache.get("q7")

    tok = get_token()
    if not tok:
        return cache.get("q5"), cache.get("q7")

    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "anthropic-beta": "oauth-2025-04-20",
                "Authorization": f"Bearer {tok}",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            q5 = data.get("five_hour", {}).get("utilization")
            q7 = data.get("seven_day", {}).get("utilization")
            save(QUOTA_CACHE, {"q5": q5, "q7": q7, "ts": now})
            return q5, q7
    except Exception:
        return cache.get("q5"), cache.get("q7")


def update_daily(sid, inp, out, cu_cache_read, cu_cache_write):
    """Update daily totals with file locking to prevent lost updates.

    cu_cache_read/cu_cache_write: per-API-call values from current_usage.
    We accumulate these across calls by detecting new API calls (total_input increased
    OR current_usage cache values changed — the latter catches fully-cached calls).
    Returns (daily_input, daily_output, daily_cache_read, daily_cache_write,
             session_cache_read, session_cache_write).
    """
    today = date.today().isoformat()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DAILY_FILE.with_suffix(".lock")

    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        d = load(DAILY_FILE)
        if d.get("date") != today:
            # Archive yesterday's totals before resetting.
            if d.get("date") and d.get("input", 0) + d.get("output", 0) > 0:
                summary = json.dumps({
                    "date": d["date"],
                    "input": d.get("input", 0),
                    "output": d.get("output", 0),
                    "cache_read": d.get("cached", 0),
                    "cache_write": d.get("cache_write", 0),
                    "sessions": len(d.get("sessions", {})),
                })
                try:
                    fd2 = os.open(str(HISTORY_FILE),
                                  os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    os.write(fd2, (summary + "\n").encode())
                    os.close(fd2)
                except Exception:
                    pass
            d = {"date": today, "sessions": {},
                 "input": 0, "output": 0, "cached": 0, "cache_write": 0}

        prev = d.get("sessions", {}).get(sid, {})

        # Detect new API call to avoid double-counting cache values
        # (statusline fires multiple times per call during streaming).
        # Primary: total_input increased. Fallback: current_usage changed
        # (catches calls where input is fully cached, so total_input stays flat).
        prev_li = prev.get("li", 0)
        prev_cu_cr = prev.get("lcr", 0)
        prev_cu_cw = prev.get("lcw", 0)
        new_call = (inp > prev_li
                    or cu_cache_read != prev_cu_cr
                    or cu_cache_write != prev_cu_cw)
        prev_cr = prev.get("c", 0)
        prev_cw = prev.get("cw", 0)
        acc_cr = prev_cr + cu_cache_read if new_call else prev_cr
        acc_cw = prev_cw + cu_cache_write if new_call else prev_cw

        di = max(0, inp - prev.get("i", 0))
        do_ = max(0, out - prev.get("o", 0))
        d_cr = max(0, acc_cr - prev_cr)
        d_cw = max(0, acc_cw - prev_cw)

        d.setdefault("sessions", {})[sid] = {
            "i": inp, "o": out, "c": acc_cr, "cw": acc_cw, "li": inp,
            "lcr": cu_cache_read, "lcw": cu_cache_write}
        d["input"] = d.get("input", 0) + di
        d["output"] = d.get("output", 0) + do_
        d["cached"] = d.get("cached", 0) + d_cr
        d["cache_write"] = d.get("cache_write", 0) + d_cw

        save(DAILY_FILE, d)
        return (d["input"], d["output"], d["cached"], d.get("cache_write", 0),
                acc_cr, acc_cw)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def energy_mid(fresh_in, cached_in, cache_write_in, out):
    """Compute mid energy estimate in mWh from token counts."""
    return (fresh_in / 1000 * E_IN
            + cached_in / 1000 * E_CACHE
            + cache_write_in / 1000 * E_CW
            + out / 1000 * E_OUT)


def fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_nrg(mwh):
    """Format energy as order-of-magnitude estimate: ~1mWh, ~10Wh, ~1kWh etc."""
    if mwh < 1:
        return "~0"
    # Snap to nearest 1, 2, 5, 10, 20, 50, ... (E-series-like steps)
    import math
    log = math.log10(mwh)
    decade = int(math.floor(log))
    frac = log - decade
    # Snap to 1, 2, 5, or 10 (at log10 positions 0, 0.3, 0.7, 1.0)
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


def load_history():
    """Load history file into a dict keyed by date string."""
    days = {}
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            if line.strip():
                try:
                    d = json.loads(line)
                    days[d["date"]] = d
                except Exception:
                    pass
    return days


def _sum_range(days, start, end):
    """Sum token fields from history entries in [start, end] date range.
    Returns (input, output, cache_read, cache_write)."""
    inp = out = cr = cw = 0
    for dt_str, entry in days.items():
        if start <= dt_str <= end:
            inp += entry.get("input", 0)
            out += entry.get("output", 0)
            cr += entry.get("cache_read", 0)
            cw += entry.get("cache_write", 0)
    return inp, out, cr, cw


def weekly_monthly_totals(d_in, d_out, d_cr, d_cw):
    """Compute W/M token totals and energy estimates."""
    today = date.today()
    days = load_history()

    # Week = Monday..today (today's live data added separately)
    monday = today - timedelta(days=today.weekday())
    w_start = monday.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    w_inp, w_out, w_cr, w_cw = _sum_range(days, w_start, yesterday)
    w_inp += d_in; w_out += d_out; w_cr += d_cr; w_cw += d_cw

    # Month = 1st of month..today
    m_start = today.replace(day=1).isoformat()
    m_inp, m_out, m_cr, m_cw = _sum_range(days, m_start, yesterday)
    m_inp += d_in; m_out += d_out; m_cr += d_cr; m_cw += d_cw

    w_mid = energy_mid(w_inp, w_cr, w_cw, w_out)
    m_mid = energy_mid(m_inp, m_cr, m_cw, m_out)

    w_str = f"W:{fmt_tok(w_inp + w_out + w_cr + w_cw)} {fmt_nrg(w_mid)}"
    m_str = f"M:{fmt_tok(m_inp + m_out + m_cr + m_cw)} {fmt_nrg(m_mid)}"
    return w_str, m_str


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        data = {}

    if DEBUG:
        try:
            entry = json.dumps({"ts": time.time(), "raw": data})
            fd = os.open(str(DEBUG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.write(fd, (entry + "\n").encode())
            os.close(fd)
        except Exception:
            pass

    model = data.get("model", {}).get("display_name", "?")
    ctx = data.get("context_window", {})
    ctx_pct = ctx.get("used_percentage")
    sid = data.get("session_id", "unknown")

    s_in = ctx.get("total_input_tokens", 0)
    s_out = ctx.get("total_output_tokens", 0)

    # Cache data lives in current_usage (per-API-call), not at top level.
    # We accumulate it across calls in update_daily.
    current_usage = ctx.get("current_usage") or {}
    cu_cache_read = current_usage.get("cache_read_input_tokens", 0)
    cu_cache_write = current_usage.get("cache_creation_input_tokens", 0)

    d_in, d_out, d_cr, d_cw, s_cr, s_cw = update_daily(
        sid, s_in, s_out, cu_cache_read, cu_cache_write)
    # total_input_tokens EXCLUDES cached tokens in Claude Code's API.
    # fresh = total_input (already fresh-only), cached is additive.
    d_mid = energy_mid(d_in, d_cr, d_cw, d_out)

    q5, q7 = fetch_quota()

    parts = [model]
    if ctx_pct is not None:
        parts.append(f"Ctx:{ctx_pct}%")
    if q5 is not None:
        q_str = f"5h:{q5:.0f}%"
        if q7 is not None:
            q_str += f" 7d:{q7:.0f}%"
        parts.append(q_str)
    parts.append(f"D:{fmt_tok(d_in + d_cr + d_cw + d_out)} {fmt_nrg(d_mid)}")

    w_str, m_str = weekly_monthly_totals(d_in, d_out, d_cr, d_cw)
    parts.append(w_str)
    parts.append(m_str)

    print(" | ".join(parts), end="")


if __name__ == "__main__":
    main()
