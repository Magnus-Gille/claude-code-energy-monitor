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
