#!/usr/bin/env python3
"""Claude Code statusline: model, context, quota, tokens, energy range, daily history.

Energy estimates show a LOW-HIGH range with ~10x total uncertainty,
based on the best available public research. No one outside Anthropic
knows the actual energy per token for Claude models — these are derived
estimates, not measurements.

Center estimates (mWh per 1k tokens) from Couch (2026), who derived
them from Epoch AI's GPT-4o analysis and Anthropic's pricing ratios:
  - Fresh input:  390   (parallel prefill)
  - Output:       1,950 (autoregressive decode, ~5x input)
  - Cache read:   39    (skips prefill, ~10x cheaper than fresh input)

We apply 3x uncertainty in each direction (divide/multiply by 3),
giving a ~10x range from low to high. This brackets:
  - Google's measured 0.24 Wh per median Gemini query (Aug 2025)
  - OpenAI's reported 0.34 Wh per avg ChatGPT query (Jun 2025)
  - Couch's derived 41 Wh per median Claude Code session (Jan 2026)

Primary sources:
  Couch (2026)    https://www.simonpcouch.com/blog/2026-01-20-cc-impact/
  Epoch AI (2025) https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use
  Google (2025)   https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference
  Luccioni (2024) arXiv:2311.16863  "Power Hungry Processing"
  Husom (2024)    arXiv:2407.16893  "The Price of Prompting"

What this does NOT capture:
  - Training energy, embodied energy, networking
  - Reasoning/extended thinking overhead (can be 150-700x, per AI Energy Score v2)
  - Geographic carbon intensity variation
  - Actual hardware, batch sizes, or optimizations used by Anthropic

For everyday comparisons (order of magnitude):
  One Google search ............. ~0.3 Wh
  One ChatGPT/Gemini query ...... ~0.3 Wh
  Charging a smartphone ......... ~15 Wh
  LED bulb for 1 hour ........... 10 Wh
  Electric oven for 1 minute .... 50 Wh
  Driving an EV 1 km ............ ~150 Wh

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
from datetime import date

CACHE_DIR = Path.home() / ".claude"
DAILY_FILE = CACHE_DIR / "statusline_daily.json"
HISTORY_FILE = CACHE_DIR / "statusline_history.jsonl"
QUOTA_CACHE = CACHE_DIR / "statusline_quota_cache.json"
QUOTA_TTL = 300  # seconds between API calls

# Energy: mWh per 1k tokens — low / high bounds
# Center from Couch (2026), +/- 3x uncertainty
E_IN_LO, E_IN_HI = 130, 1170          # fresh input
E_OUT_LO, E_OUT_HI = 650, 5850        # output (decode)
E_CACHE_LO, E_CACHE_HI = 13, 117      # cached input (cache read)


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


def update_daily(sid, inp, out, cu_cache_read):
    """Update daily totals with file locking to prevent lost updates.

    cu_cache_read: cache_read_input_tokens from current_usage (per-API-call).
    We accumulate this across calls by detecting new API calls (total_input increased).
    Returns (daily_input, daily_output, daily_cached, session_cached).
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
                    "cached": d.get("cached", 0),
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
                 "input": 0, "output": 0, "cached": 0}

        prev = d.get("sessions", {}).get(sid, {})

        # Detect new API call: total_input_tokens increased since last seen.
        # Only accumulate cache_read on new calls to avoid double-counting
        # (statusline fires multiple times per call during streaming).
        prev_li = prev.get("li", 0)
        new_call = inp > prev_li
        prev_cached = prev.get("c", 0)
        accumulated_cache = prev_cached + cu_cache_read if new_call else prev_cached

        di = max(0, inp - prev.get("i", 0))
        do_ = max(0, out - prev.get("o", 0))
        dc = max(0, accumulated_cache - prev_cached)

        d.setdefault("sessions", {})[sid] = {
            "i": inp, "o": out, "c": accumulated_cache, "li": inp}
        d["input"] = d.get("input", 0) + di
        d["output"] = d.get("output", 0) + do_
        d["cached"] = d.get("cached", 0) + dc

        save(DAILY_FILE, d)
        return d["input"], d["output"], d["cached"], accumulated_cache
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def energy_range(fresh_in, cached_in, out):
    """Compute low/high energy in mWh from token counts."""
    lo = (fresh_in / 1000 * E_IN_LO
          + cached_in / 1000 * E_CACHE_LO
          + out / 1000 * E_OUT_LO)
    hi = (fresh_in / 1000 * E_IN_HI
          + cached_in / 1000 * E_CACHE_HI
          + out / 1000 * E_OUT_HI)
    return lo, hi


def fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_nrg_range(lo_mwh, hi_mwh):
    """Format energy range, normalizing both values to the same unit."""
    if hi_mwh < 1:
        return "<1mWh"
    if hi_mwh < 1000:
        lo_s = f"{lo_mwh:.0f}" if lo_mwh >= 1 else "<1"
        return f"{lo_s}-{hi_mwh:.0f}mWh"
    lo_wh = lo_mwh / 1000
    hi_wh = hi_mwh / 1000
    if hi_wh < 1000:
        lo_s = f"{lo_wh:.1f}" if lo_wh < 10 else f"{lo_wh:.0f}"
        hi_s = f"{hi_wh:.1f}" if hi_wh < 10 else f"{hi_wh:.0f}"
        return f"{lo_s}-{hi_s}Wh"
    lo_kwh = lo_wh / 1000
    hi_kwh = hi_wh / 1000
    return f"{lo_kwh:.1f}-{hi_kwh:.1f}kWh"


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        data = {}

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

    d_in, d_out, d_cached, s_cached = update_daily(
        sid, s_in, s_out, cu_cache_read)
    # total_input_tokens EXCLUDES cached tokens in Claude Code's API.
    # fresh = total_input (already fresh-only), cached is additive.
    s_fresh = s_in
    d_fresh = d_in

    s_lo, s_hi = energy_range(s_fresh, s_cached, s_out)
    d_lo, d_hi = energy_range(d_fresh, d_cached, d_out)

    q5, q7 = fetch_quota()

    parts = [model]
    if ctx_pct is not None:
        parts.append(f"Ctx:{ctx_pct}%")
    if q5 is not None:
        q_str = f"5h:{q5:.0f}%"
        if q7 is not None:
            q_str += f" 7d:{q7:.0f}%"
        parts.append(q_str)
    parts.append(f"S:{fmt_tok(s_in + s_cached + s_out)} {fmt_nrg_range(s_lo, s_hi)}")
    parts.append(f"D:{fmt_tok(d_in + d_cached + d_out)} {fmt_nrg_range(d_lo, d_hi)}")

    print(" | ".join(parts), end="")


if __name__ == "__main__":
    main()
