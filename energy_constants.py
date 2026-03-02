"""Canonical energy constants for Claude Code Energy Monitor.

This is the single source of truth for energy-per-token estimates.
statusline.py embeds its own copy for single-file deployment — keep in sync.

Values in mWh per 1,000 tokens. Revised via adversarial debate (Claude vs
Codex, Feb 2026) using physics-derived cross-checks against pricing-only
estimates. See docs/energy-constants.md for full rationale.
"""

E_IN = 390       # fresh input (prefill) — Epoch AI long-context anchor
E_OUT = 1400     # output (decode) — cross-checks cluster 600–1,800
E_CACHE = 15     # cached input (cache read) — ~26x discount, physics-derived
E_CW = 490       # cache creation (write) — prefill + write overhead
