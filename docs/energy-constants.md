# Energy Constants Rationale

Last updated: 2026-02-26

## Current values

| Token type | mWh / 1k tokens | Source |
|---|---|---|
| Fresh input (prefill) | 390 | Epoch AI long-context anchor via Couch (2026) |
| Output (decode) | 1,400 | Revised down from 1,950; cross-checks cluster 600-1,800 |
| Cache read | 15 | Revised down from 39; physics-derived ~26x discount vs input |
| Cache write | 490 | Prefill + 25% write overhead (unchanged from Couch) |

All values carry at least +/-3x uncertainty. The display intentionally snaps to order-of-magnitude steps (1/2/5 per decade).

## What changed and why

The original constants were derived by Couch (2026) from Epoch AI's GPT-4o energy measurements, scaled to Claude using Anthropic's pricing ratios. Two constants were revised after adversarial debate (Claude Opus 4.6 vs GPT-5.3-Codex, Feb 2026):

**Output: 1,950 -> 1,400 mWh/1k tokens.** The original used Anthropic's 5:1 output/input pricing ratio, but pricing reflects margin and demand management, not just energy. Independent cross-checks converge lower:
- FLOP-based estimate for 200B-class model: 750-1,500
- AI Energy Score v2 benchmarks (scaled from 70B): ~1,200
- Llama 405B measured (batched, with overhead): ~2,800
- Most estimates cluster 1,000-1,500. Codex pushed for 1,400 over Claude's proposed 1,200.

**Cache read: 39 -> 15 mWh/1k tokens.** The original used Anthropic's 10:1 pricing discount, but the physics discount is much larger. Cached tokens skip prefill entirely -- the cost is loading pre-computed KV pairs from memory plus attention during decode. Physics analysis supports 26-1,000x discounts; the 26x (15 mWh) is a conservative compromise that accounts for ongoing decode attention cost over cached context.

**Unchanged: fresh input (390) and cache write (490).** The Epoch AI anchor for long-context prefill remains the best available estimate. Cache write = prefill + 25% overhead, matching Anthropic's pricing surcharge.

## Cross-checks

| Source | Value | Notes |
|---|---|---|
| Google (2025) | 0.24 Wh / median Gemini query | Full-stack including idle + PUE |
| OpenAI (2025) | 0.34 Wh / average ChatGPT query | |
| Epoch AI (2025) | ~0.3 Wh / GPT-4o query | Empirical |
| AI Energy Score v2 | ~600 mWh/1k output for 70B | Scaled to ~1,200 for 200B+ |
| Llama 405B measured | ~2,800 mWh/1k output | Batched, with overhead |

## Key uncertainties

1. **Model size.** Same constants for Haiku/Sonnet/Opus despite ~2-5x size differences.
2. **Context-length effects.** Fixed per-token output cost, but decode cost grows with context length (larger KV-cache attention). Underestimates in long-context sessions.
3. **Infrastructure unknowns.** Anthropic's hardware, batch sizes, scheduling, datacenter locations are all unknown. Inference efficiency is a moving target (Google reported 33x improvement in one year).
4. **Pricing != energy.** Fresh input and cache write constants still inherit from pricing ratios. These may over- or under-estimate actual energy.

## Debate process

Constants were stress-tested via two rounds of adversarial debate between Claude Opus 4.6 and GPT-5.3-Codex. Both models agreed on the direction of all changes; disagreements were on magnitude (e.g., cache read discount: 50x vs 26x, output: 1,200 vs 1,400). The shipped values reflect the more conservative position in each case.

Full debate transcripts are in the `debate/` directory (gitignored due to size).

## 2026-05-30 audit update

A deep-dive audit (Claude Opus 4.8, Claude Code v2.1.157) re-checked the constants and the data path against ~3 months of model/CC changes and new 2026 energy literature. Headline: the token-counting validation from Feb 2026 had silently gone stale, and the workload had inverted. Findings below were adversarially verified against primary sources.

### Token accounting broke (and is now fixed)

As of **Claude Code v2.1.122**, `context_window.total_input_tokens` and `total_output_tokens` changed from *cumulative session counters* to *current-context snapshots*: `total_input_tokens` is now `current_usage.input_tokens + cache_creation + cache_read` of the most recent response, and `total_output_tokens` equals that response's `current_usage.output_tokens` (resets each call). The Feb 2026 monitor assumed both were monotonic cumulative counters, so its delta logic broke. Confirmed empirically (ENERGY_DEBUG capture, v2.1.157) and replay-validated against ground truth:

- **Fresh input was over-counted ~53x** — the old delta of `total_input_tokens` was tracking *context growth* (cache-dominated), not fresh input. True fresh (uncached) input is tiny in a cached workload (most prefill work is `cache_creation`).
- **Output was under-counted** — `max(0, total_output − prev)` clamps to ~0 once `total_output` resets per call.
- **Cache read/write were unaffected** (already accumulated from `current_usage`).

Fix (`update_daily`): accumulate the per-call `current_usage` fields, detecting call boundaries (input-side signature change *or* output reset) and summing each call once. Fresh input is derived as `total_input − cache_read − cache_creation`. Net effect: the input energy term drops from a (spurious) ~12–15% share to ~1%; the real prefill work is correctly attributed to `cache_write`.

### Workload inverted

In Feb 2026 (long interactive Opus sessions) output dominated energy (~46%). By May 2026 the workload is ~300 short automated sessions/day (a Raspberry-Pi fleet), and **`cache_write` is the largest term** (~34% full-history, ~40% last-30-days). The shift was driven less by cache-write volume (+9%) than by a 23% drop in `cache_read` reuse (short sessions create caches read fewer times before expiry).

Sensitivity (last-30-day total): `E_CW` over {390,490,780} moves it −8%/+24%; `E_CACHE` over {5,15,30} moves it −19%/+28%. **`E_CACHE` is the single most leverage-sensitive constant** (cache_read volume is ~23x cache_write volume), despite its small per-token value — worth remembering that it carries a 15 (physics) vs 39 (pricing) ambiguity.

### E_CW relabeled (value unchanged)

No 2026 paper measures KV-cache *creation* energy separately from prefill. Physically, cache creation is a normal prefill (same FLOPs) plus a negligible HBM write; the +25% in `E_CW = 490` is a pricing/infrastructure surcharge, not a measured GPU-energy delta. Kept at 490 for continuity but **relabeled as "prefill compute + pricing-derived infra surcharge proxy"** rather than compute-only. (Also note Anthropic now has two cache-write tiers: 5-min = 1.25× input, 1-hour = 2.0×. Claude Code uses 5-min for subagents and 1-hour for main turns, so a pricing-faithful `E_CW` would actually span 490–780; the statusline aggregate can't distinguish tiers, only the raw JSONL `ephemeral_5m`/`ephemeral_1h` fields can.)

### Per-model multipliers added

Constants were Opus-anchored, but the fleet is now mixed (measured: **Opus ~56%, Sonnet ~43%, Haiku ~1%**). Added order-of-magnitude multipliers relative to Opus: **Haiku 0.3, Sonnet 0.6, Opus 1.0**. Basis: input-price ratio (1:3:5) discounted for sub-linear param→energy scaling (verified: LLaMA-3 1B→70B = 7.3× energy for 70× params, arXiv:2512.03024) and larger batches on cheaper tiers. These are guesses — Anthropic discloses no parameter counts. The statusline weights today's data and all future days; legacy history days (no per-model breakdown) fall back to 1.0×.

### New 2026 evidence (verified) — does it move the constants?

| Source (date) | Finding | Bearing on constants |
|---|---|---|
| Oviedo et al., *Joule* 2026 (arXiv:2509.20241) | Frontier >200B median 0.34 Wh/query (IQR 0.18–0.67); bottom-up TDP estimates overstate 4–20× | Full-stack output ≈ 1,100–1,360 mWh/1k at PUE 1.2–1.4 → **brackets E_OUT=1400**. Strengthens "order-of-magnitude only." |
| ML.ENERGY Leaderboard v3.0 (Jan 2026) | Qwen-3-32B on **B200** 0.15–0.31 J/token; B200 ≈ **−35%** vs H100 | Best live per-token anchor (open models). Hardware generation is a systematic drift source — if Anthropic runs Blackwell, all constants ~35% high. |
| Uptime Institute (May 2026) | Llama-3.3-70B H100 0.77–3.5 J/token full-stack, utilization-driven 4.5× range | Utilization swamps model uncertainty; E_OUT plausible at low utilization, maybe 2× high at high utilization. |
| Jegham et al. (arXiv:2505.09598) | Claude-3.7-Sonnet 0.95 / 2.99 / 5.67 Wh (short/med/long), full-stack PUE 1.12–1.27 | Closest Claude-specific point; broadly consistent with E_OUT within ±3×. |
| Anthropic pricing/docs (May 2026) | Opus 4.6→4.7 (Apr 16)→4.8 (May 28), pricing unchanged $5/$25; **new tokenizer +0–35% tokens** (4.7+); adaptive-only thinking; **Opus 4.8 cache minimum 1,024** (was 4,096) | Per-token constants unchanged. But same task yields more tokens on 4.8 (tokenizer) — confounds cross-time energy-per-task comparisons. Lower cache minimum → more cache_creation events on short prompts → amplifies E_CW share. |

**Net:** no constant value changed (all sit within the stated ±3×). The accuracy gains came from fixing the token accounting and adding per-model weighting, not from re-tuning constants. `E_CW` and `E_CACHE` remain the least-validated / most-sensitive and are the priorities if better data appears.

### Operational notes

- The quota source switched from the undocumented `/api/oauth/usage` endpoint (now rate-limited to persistent 429s) to the statusline payload's `rate_limits.{five_hour,seven_day}.used_percentage` (CC v2.1.80+, no API call).
- Anthropic still publishes no per-token/per-query energy and no sustainability report; the derivation chain remains the best available approach.
