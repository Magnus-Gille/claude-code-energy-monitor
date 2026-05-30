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
E_CW = 490       # cache creation (write) = prefill compute (~390) + pricing-derived
                 # infrastructure surcharge proxy (1.25x). NOT pure compute energy:
                 # KV-cache creation has the same FLOPs as a normal prefill, so the
                 # +25% is a pricing/infra carryover, not a measured GPU-energy delta.
                 # Kept at 490 for continuity; treat as "prefill + infra proxy".

# Per-model energy multipliers (relative to the Opus-anchored constants above).
# Order-of-magnitude only: the constants were derived for an Opus-class model, but
# the fleet now mixes Haiku/Sonnet/Opus. Basis is the input-price ratio
# (Haiku:Sonnet:Opus = 1:3:5), discounted for sub-linear param->energy scaling
# (e.g. LLaMA-3 1B->70B = 7.3x energy for 70x params) and larger batches on
# cheaper tiers. These are guesses, not measurements — Anthropic discloses no
# parameter counts. See docs/energy-constants.md.
MODEL_MULTIPLIERS = {"haiku": 0.3, "sonnet": 0.6, "opus": 1.0}


def model_tier(model_id):
    """Map a model id (e.g. 'claude-opus-4-8') to a tier key. Unknown -> 'opus'
    (conservative: assume the most energy-intensive tier)."""
    m = (model_id or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "opus"


def model_multiplier(model_id):
    """Energy multiplier for a model id (defaults to 1.0 = Opus-class)."""
    return MODEL_MULTIPLIERS.get(model_tier(model_id), 1.0)


def energy_mwh(fresh_in, cached_in, cache_write_in, out, multiplier=1.0):
    """Energy estimate in mWh from token counts, scaled by a model multiplier."""
    return multiplier * (fresh_in / 1000 * E_IN
                         + cached_in / 1000 * E_CACHE
                         + cache_write_in / 1000 * E_CW
                         + out / 1000 * E_OUT)
