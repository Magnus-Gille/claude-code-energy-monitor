#!/usr/bin/env python3
"""Quick API test: make a known call, capture usage, compute energy estimate.

Run:  ANTHROPIC_API_KEY=sk-... python3 api_test.py
Then compare token counts and cost against https://console.anthropic.com/settings/usage
"""

import json
import os
import sys
import time
import urllib.request

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("Set ANTHROPIC_API_KEY env var first")
    sys.exit(1)

MODEL = "claude-sonnet-4-20250514"

# Same energy constants as statusline.py (mWh per 1k tokens)
E_IN_LO, E_IN_HI = 130, 1170       # fresh input
E_OUT_LO, E_OUT_HI = 650, 5850     # output
E_CACHE_LO, E_CACHE_HI = 13, 117   # cache read
E_CW_LO, E_CW_HI = 163, 1470      # cache write

# Sonnet pricing (per 1M tokens)
PRICE_INPUT = 3.00
PRICE_OUTPUT = 15.00
PRICE_CACHE_READ = 0.30
PRICE_CACHE_WRITE = 3.75


def energy_range(fresh_in, cached_in, cache_write_in, out):
    lo = (fresh_in / 1000 * E_IN_LO + cached_in / 1000 * E_CACHE_LO
          + cache_write_in / 1000 * E_CW_LO + out / 1000 * E_OUT_LO)
    hi = (fresh_in / 1000 * E_IN_HI + cached_in / 1000 * E_CACHE_HI
          + cache_write_in / 1000 * E_CW_HI + out / 1000 * E_OUT_HI)
    return lo, hi


def cost_usd(fresh_in, cached_in, cache_write_in, out):
    return (fresh_in * PRICE_INPUT + out * PRICE_OUTPUT
            + cached_in * PRICE_CACHE_READ
            + cache_write_in * PRICE_CACHE_WRITE) / 1_000_000


def call_api(prompt, system=None, use_cache=False):
    """Make a single API call and return usage dict."""
    messages = [{"role": "user", "content": prompt}]
    body = {"model": MODEL, "max_tokens": 1024, "messages": messages}
    if system:
        if use_cache:
            body["system"] = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
        else:
            body["system"] = system

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def print_result(label, resp):
    usage = resp.get("usage", {})
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    cw = usage.get("cache_creation_input_tokens", 0)

    # Fresh input = input_tokens (API already excludes cache from this)
    fresh = inp
    lo, hi = energy_range(fresh, cr, cw, out)
    usd = cost_usd(fresh, cr, cw, out)

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Model:          {resp.get('model', '?')}")
    print(f"  Input tokens:   {inp:,}  (fresh)")
    print(f"  Output tokens:  {out:,}")
    print(f"  Cache read:     {cr:,}")
    print(f"  Cache write:    {cw:,}")
    print(f"  ---")
    print(f"  Total tokens:   {inp + out + cr + cw:,}")
    print(f"  Est. cost:      ${usd:.6f}")
    print(f"  Energy range:   {lo:.1f} - {hi:.1f} mWh")
    if hi > 1000:
        print(f"                  {lo/1000:.2f} - {hi/1000:.2f} Wh")
    print(f"  Stop reason:    {resp.get('stop_reason', '?')}")

    return {"fresh": fresh, "out": out, "cr": cr, "cw": cw, "cost": usd, "lo": lo, "hi": hi}


def main():
    print(f"Model: {MODEL}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    totals = {"fresh": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0, "lo": 0, "hi": 0}

    # --- Test 1: Simple short call ---
    r1 = call_api("Say exactly: 'Hello, energy test!' — nothing else.")
    t1 = print_result("Test 1: Simple short call", r1)

    # --- Test 2: Longer output ---
    r2 = call_api("List the planets in the solar system with one fun fact each. Be concise.")
    t2 = print_result("Test 2: Longer output", r2)

    # --- Test 3: Cached system prompt (first call = cache write) ---
    big_system = "You are an expert astronomer. " * 200  # ~1k tokens of system prompt
    r3 = call_api("Name one constellation.", system=big_system, use_cache=True)
    t3 = print_result("Test 3: Cache write (first call)", r3)

    # --- Test 4: Cached system prompt (second call = cache read) ---
    time.sleep(1)
    r4 = call_api("Name another constellation.", system=big_system, use_cache=True)
    t4 = print_result("Test 4: Cache read (second call)", r4)

    # --- Totals ---
    for t in [t1, t2, t3, t4]:
        for k in totals:
            totals[k] += t[k]

    print(f"\n{'='*60}")
    print(f"  TOTALS across all 4 calls")
    print(f"{'='*60}")
    print(f"  Fresh input:    {totals['fresh']:,}")
    print(f"  Output:         {totals['out']:,}")
    print(f"  Cache read:     {totals['cr']:,}")
    print(f"  Cache write:    {totals['cw']:,}")
    print(f"  ---")
    print(f"  Est. cost:      ${totals['cost']:.6f}")
    print(f"  Energy range:   {totals['lo']:.1f} - {totals['hi']:.1f} mWh")
    if totals['hi'] > 1000:
        print(f"                  {totals['lo']/1000:.2f} - {totals['hi']/1000:.2f} Wh")
    print(f"\nCompare these numbers at: https://console.anthropic.com/settings/usage")


if __name__ == "__main__":
    main()
