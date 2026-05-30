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
  - Distinct energy weighting for reasoning/thinking tokens — they ARE counted
    (within output tokens), but priced the same as visible output
  - Geographic carbon intensity variation
  - Actual hardware, batch sizes, or optimizations used by Anthropic

NOTE: Quota is read from the statusline payload's rate_limits fields
(Claude Code v2.1.80+; no API call). If those are absent it falls back to
an UNDOCUMENTED Anthropic beta endpoint (/api/oauth/usage with
anthropic-beta: oauth-2025-04-20), which may change or disappear without notice.
"""

import fcntl
import hashlib
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
SESSION_HISTORY_FILE = CACHE_DIR / "statusline_session_history.jsonl"
QUOTA_CACHE = CACHE_DIR / "statusline_quota_cache.json"
QUOTA_TTL = 300  # seconds between API calls
DEBUG_FILE = CACHE_DIR / "statusline_debug.jsonl"
DEBUG = os.environ.get("ENERGY_DEBUG", "") == "1"
# Short content hash of this script — stamps data so we know which version produced it.
_SELF_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]

# Energy: mWh per 1k tokens — mid estimates.
# Canonical source: energy_constants.py — keep in sync.
# Hybrid constants from Couch (2026) base + physics-derived cache/output
# adjustments via adversarial debate (see docs/energy-constants.md).
# Fresh input: Epoch AI long-context anchor (unchanged)
# Output: reduced from 1950→1400 (cross-checks cluster 600-1800)
# Cache read: reduced from 39→15 (~26x discount vs input; pricing 10x was too conservative)
# Cache write: 490 = prefill compute (~390) + pricing-derived infra surcharge proxy
#   (1.25x). KV-cache creation has the same FLOPs as a normal prefill, so the +25%
#   is a pricing/infra carryover, not a measured GPU-energy delta. Treat accordingly.
E_IN = 390      # fresh input (long-context workload)
E_OUT = 1400    # output (decode)
E_CACHE = 15    # cached input (cache read)
E_CW = 490      # cache creation (write) — prefill + infra surcharge proxy

# Per-model energy multipliers (relative to the Opus-anchored constants above).
# Order-of-magnitude only — basis is the input-price ratio (Haiku:Sonnet:Opus = 1:3:5)
# discounted for sub-linear param->energy scaling and larger batches on cheaper tiers.
# Anthropic discloses no parameter counts; these are guesses. See docs/energy-constants.md.
MODEL_MULTIPLIERS = {"haiku": 0.3, "sonnet": 0.6, "opus": 1.0}


def model_tier(model_id):
    m = (model_id or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "opus"  # unknown -> conservative (most energy-intensive)


def model_mult(model_id):
    return MODEL_MULTIPLIERS.get(model_tier(model_id), 1.0)


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


def update_daily(sid, total_in, cu_input, cu_output, cu_cache_read, cu_cache_write,
                  model_id="?", project="?", ctx_size=0, ctx_pct=0,
                  cost_usd=0):
    """Accumulate daily token totals from per-call current_usage values.

    As of Claude Code v2.1.122 the statusline's context_window.total_input_tokens
    and total_output_tokens became CURRENT-CONTEXT snapshots (input+cache_read+
    cache_creation for the latest response; output of the latest response) rather
    than cumulative session totals. So we accumulate the per-call current_usage
    fields, detecting call boundaries and summing each call once:
      - fresh (uncached) input = total_in - cache_read - cache_creation
        (equals current_usage.input_tokens, which is otherwise a near-placeholder)
      - cache_read / cache_creation: stable within a call, added once per new call
      - output: streams up within a call, so we add the per-fire positive delta
    A new call is detected when the input-side signature changes (total_in grows,
    or cache_read/creation change) OR output resets below the previous value (which
    also catches two consecutive identical fully-cached calls). Returns the daily
    dict d.
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
                    "by_model": d.get("by_model", {}),
                    "sessions": len(d.get("sessions", {})),
                    "v": d.get("v", ""),
                })
                try:
                    fd2 = os.open(str(HISTORY_FILE),
                                  os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    os.write(fd2, (summary + "\n").encode())
                    os.close(fd2)
                except Exception:
                    pass
            # Archive per-session records for advisor analysis.
            try:
                sess = d.get("sessions", {})
                if sess:
                    lines = []
                    for s_id, s_val in sess.items():
                        rec = {
                            "date": d["date"], "sid": s_id,
                            "m": s_val.get("m", "?"),
                            "p": s_val.get("p", "?"),
                            "cws": s_val.get("cws", 0),
                            "cpk": s_val.get("cpk", 0),
                            "$": s_val.get("$", 0),
                            "n": s_val.get("n", 0),
                            "i": s_val.get("i", 0),
                            "o": s_val.get("o", 0),
                            "c": s_val.get("c", 0),
                            "cw": s_val.get("cw", 0),
                            # Daily deltas for this session
                            "di": s_val.get("di", 0),
                            "do": s_val.get("do", 0),
                            "dc": s_val.get("dc", 0),
                            "dcw": s_val.get("dcw", 0),
                            "fs": s_val.get("fs", 0),
                            "ls": s_val.get("ls", 0),
                        }
                        lines.append(json.dumps(rec))
                    fd3 = os.open(str(SESSION_HISTORY_FILE),
                                  os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    os.write(fd3, ("\n".join(lines) + "\n").encode())
                    os.close(fd3)
            except Exception:
                pass
            # Carry forward session baselines so resumed sessions
            # don't attribute their full history to the new day. li/lcr/lcw/lout
            # MUST carry over so the first fire of the new day for a continuing
            # call is not mis-detected as a fresh call (which would double-count).
            baselines = {}
            for s_id, s_val in d.get("sessions", {}).items():
                # Prune stale baselines: no metadata and no activity today
                if (s_val.get("m", "?") == "?"
                        and s_val.get("n", 0) == 0):
                    continue
                baselines[s_id] = {
                    "i": s_val.get("i", 0), "o": s_val.get("o", 0),
                    "c": s_val.get("c", 0), "cw": s_val.get("cw", 0),
                    "li": s_val.get("li", 0),
                    "lcr": s_val.get("lcr", 0), "lcw": s_val.get("lcw", 0),
                    "lout": s_val.get("lout", 0),
                    # Reset daily deltas for new day
                    "di": 0, "do": 0, "dc": 0, "dcw": 0,
                    "m": s_val.get("m", "?"), "p": s_val.get("p", "?"),
                    "cws": s_val.get("cws", 0), "cpk": 0,
                    "$": s_val.get("$", 0), "n": 0,
                    "fs": s_val.get("fs", 0), "ls": s_val.get("ls", 0),
                }
            d = {"date": today, "sessions": baselines,
                 "input": 0, "output": 0, "cached": 0, "cache_write": 0,
                 "by_model": {}, "v": _SELF_HASH}

        d.setdefault("by_model", {})
        prev = d.get("sessions", {}).get(sid, {})

        # Fresh (uncached, non-written) input for this call, derived from the
        # current-context total. Robust whether or not current_usage.input_tokens
        # is populated (it is often a near-placeholder of 1-2).
        cu_fresh = max(0, total_in - cu_cache_read - cu_cache_write)

        prev_li = prev.get("li", 0)
        prev_cr = prev.get("lcr", 0)
        prev_cw = prev.get("lcw", 0)
        prev_out = prev.get("lout", 0)

        new_call = (total_in > prev_li
                    or cu_cache_read != prev_cr
                    or cu_cache_write != prev_cw
                    or cu_output < prev_out)

        if new_call:
            add_fresh, add_cr, add_cw, add_out = (
                cu_fresh, cu_cache_read, cu_cache_write, cu_output)
        else:
            add_fresh = add_cr = add_cw = 0
            add_out = max(0, cu_output - prev_out)  # streaming growth within a call

        now = time.time()
        s = d.setdefault("sessions", {}).setdefault(sid, {})
        s["i"] = s.get("i", 0) + add_fresh   # session-lifetime accumulators
        s["o"] = s.get("o", 0) + add_out
        s["c"] = s.get("c", 0) + add_cr
        s["cw"] = s.get("cw", 0) + add_cw
        s["di"] = s.get("di", 0) + add_fresh  # daily deltas (reset at midnight)
        s["do"] = s.get("do", 0) + add_out
        s["dc"] = s.get("dc", 0) + add_cr
        s["dcw"] = s.get("dcw", 0) + add_cw
        s["li"] = total_in
        s["lcr"] = cu_cache_read
        s["lcw"] = cu_cache_write
        s["lout"] = cu_output
        s["m"] = model_id
        s["p"] = project
        s["cws"] = ctx_size
        s["cpk"] = max(prev.get("cpk", 0), ctx_pct or 0)
        s["$"] = cost_usd
        s["n"] = prev.get("n", 0) + (1 if new_call else 0)
        s["fs"] = prev.get("fs", now)
        s["ls"] = now

        d["input"] = d.get("input", 0) + add_fresh
        d["output"] = d.get("output", 0) + add_out
        d["cached"] = d.get("cached", 0) + add_cr
        d["cache_write"] = d.get("cache_write", 0) + add_cw

        tier = model_tier(model_id)
        bm = d["by_model"].setdefault(
            tier, {"input": 0, "output": 0, "cached": 0, "cache_write": 0})
        bm["input"] += add_fresh
        bm["output"] += add_out
        bm["cached"] += add_cr
        bm["cache_write"] += add_cw

        save(DAILY_FILE, d)
        return d
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def energy_mid(fresh_in, cached_in, cache_write_in, out):
    """Compute mid energy estimate in mWh from token counts (model-agnostic)."""
    return (fresh_in / 1000 * E_IN
            + cached_in / 1000 * E_CACHE
            + cache_write_in / 1000 * E_CW
            + out / 1000 * E_OUT)


def energy_for_day(day):
    """Mid energy (mWh) for one day dict, model-weighted when a per-model
    breakdown ('by_model') is present. Any top-level tokens NOT represented in
    by_model are priced model-agnostically as a residual — this matters on
    mixed-format days (e.g. the per-model upgrade transition, where the morning's
    totals were written without by_model and only later calls are per-model).
    Legacy days (no by_model) are fully agnostic. Accepts both the live daily
    dict (cache key 'cached') and archived history rows (key 'cache_read')."""
    cr_total = day.get("cache_read", day.get("cached", 0))
    bm = day.get("by_model") or {}
    total = 0.0
    acc_in = acc_out = acc_cr = acc_cw = 0
    for tier, t in bm.items():
        total += MODEL_MULTIPLIERS.get(tier, 1.0) * energy_mid(
            t.get("input", 0), t.get("cached", 0),
            t.get("cache_write", 0), t.get("output", 0))
        acc_in += t.get("input", 0); acc_out += t.get("output", 0)
        acc_cr += t.get("cached", 0); acc_cw += t.get("cache_write", 0)
    # Residual (legacy same-day totals / unmodeled data) priced agnostically.
    total += energy_mid(max(0, day.get("input", 0) - acc_in),
                        max(0, cr_total - acc_cr),
                        max(0, day.get("cache_write", 0) - acc_cw),
                        max(0, day.get("output", 0) - acc_out))
    return total


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


def weekly_monthly_totals(today_dict):
    """Compute W/M token totals and (model-weighted) energy estimates.

    Energy is summed per day via energy_for_day(), so each day applies its own
    per-model multipliers where available; legacy history days fall back to the
    model-agnostic estimate. today_dict is the live daily dict from update_daily."""
    today = date.today()
    days = load_history()
    # Inject today's live data (overrides any stale history entry for today).
    days[today.isoformat()] = {
        "date": today.isoformat(),
        "input": today_dict.get("input", 0),
        "output": today_dict.get("output", 0),
        "cache_read": today_dict.get("cached", 0),
        "cache_write": today_dict.get("cache_write", 0),
        "by_model": today_dict.get("by_model"),
    }

    end = today.isoformat()

    def window(start):
        tok = 0
        energy = 0.0
        for ds, day in days.items():
            if start <= ds <= end:
                tok += (day.get("input", 0) + day.get("output", 0)
                        + day.get("cache_read", 0) + day.get("cache_write", 0))
                energy += energy_for_day(day)
        return tok, energy

    w_tok, w_e = window((today - timedelta(days=6)).isoformat())
    m_tok, m_e = window((today - timedelta(days=29)).isoformat())
    w_str = f"W:{fmt_tok(w_tok)} {fmt_nrg(w_e)}"
    m_str = f"M:{fmt_tok(m_tok)} {fmt_nrg(m_e)}"
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
    model_id = data.get("model", {}).get("id", "?")
    ctx = data.get("context_window", {})
    ctx_pct = ctx.get("used_percentage")
    ctx_size = ctx.get("context_window_size", 0)
    sid = data.get("session_id", "unknown")
    project = os.path.basename(
        data.get("workspace", {}).get("project_dir", "?"))
    cost_usd = data.get("cost", {}).get("total_cost_usd", 0)

    # As of Claude Code v2.1.122, context_window.total_input_tokens is the
    # CURRENT-CONTEXT total (input + cache_read + cache_creation of the most
    # recent response), and total_output_tokens is that response's output —
    # neither is a cumulative session counter anymore. We accumulate the
    # per-call current_usage fields instead (see update_daily).
    total_in = ctx.get("total_input_tokens", 0)
    current_usage = ctx.get("current_usage") or {}
    cu_input = current_usage.get("input_tokens", 0)
    cu_output = current_usage.get("output_tokens", 0)
    cu_cache_read = current_usage.get("cache_read_input_tokens", 0)
    cu_cache_write = current_usage.get("cache_creation_input_tokens", 0)

    d = update_daily(
        sid, total_in, cu_input, cu_output, cu_cache_read, cu_cache_write,
        model_id=model_id, project=project, ctx_size=ctx_size,
        ctx_pct=ctx_pct or 0, cost_usd=cost_usd)
    d_tok = (d.get("input", 0) + d.get("output", 0)
             + d.get("cached", 0) + d.get("cache_write", 0))
    d_mid = energy_for_day(d)  # model-weighted via d["by_model"]

    # Quota: prefer the statusline payload's rate_limits (v2.1.80+, no API call).
    # Fall back to the undocumented OAuth usage endpoint if absent.
    rl = data.get("rate_limits") or {}
    q5 = (rl.get("five_hour") or {}).get("used_percentage")
    q7 = (rl.get("seven_day") or {}).get("used_percentage")
    if q5 is not None or q7 is not None:
        # Keep statusline_quota_cache.json fresh — advisor.py reads only that file.
        save(QUOTA_CACHE, {"q5": q5, "q7": q7, "ts": time.time()})
    else:
        q5, q7 = fetch_quota()

    parts = [model]
    if ctx_pct is not None:
        parts.append(f"Ctx:{ctx_pct}%")
    if q5 is not None:
        q_str = f"5h:{q5:.0f}%"
        if q7 is not None:
            q_str += f" 7d:{q7:.0f}%"
        parts.append(q_str)
    parts.append(f"D:{fmt_tok(d_tok)} {fmt_nrg(d_mid)}")

    w_str, m_str = weekly_monthly_totals(d)
    parts.append(w_str)
    parts.append(m_str)

    print(" | ".join(parts), end="")


if __name__ == "__main__":
    main()
