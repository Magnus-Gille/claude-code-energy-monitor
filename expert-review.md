# Claude Code Energy Monitor — Expert Review Package

**Author:** Magnus Gille (magnus.gille@outlook.com)
**Date:** 2026-02-24
**Repo:** https://github.com/Magnus-Gille/claude-code-energy-monitor

I built a statusline script for Claude Code that estimates the energy consumption of my AI-assisted coding sessions in real time. During development, I discovered several things about how Claude Code reports token usage. I'd appreciate your expert review of our methodology, conclusions, and their implications.

---

## 1. What the tool does

The script runs as a Claude Code statusline extension. On every API call, Claude Code pipes a JSON object with session data into the script's stdin. The script reads token counts, computes energy estimates, accumulates daily totals, and displays a one-line summary:

```
Opus 4.6 | Ctx:47% | 5h:29% 7d:52% | S:124k 7-64Wh | D:2.0M 0.5-4.2kWh
```

It tracks four token types separately: fresh input (prefill), output (decode), cached input (cache read), and cache creation (cache write). Daily totals persist across sessions via a JSON file with file locking for concurrent session safety.

---

## 2. Energy estimation methodology

### 2.1 Source chain

The per-token energy constants come from a chain of derived estimates:

1. **Epoch AI (2025)** measured GPT-4o inference energy empirically
2. **Couch (2026)** mapped those measurements to Claude using Anthropic's pricing ratios as a proxy for relative compute cost
3. **We** apply Couch's constants with a ±3x uncertainty band

### 2.2 Constants

| Token type | Low | Center | High | Unit | Derivation rationale |
|---|---|---|---|---|---|
| Fresh input (prefill) | 130 | 390 | 1,170 | mWh/1k tokens | Parallel GPU prefill; base from Epoch AI GPT-4o analysis |
| Output (decode) | 650 | 1,950 | 5,850 | mWh/1k tokens | Serial autoregressive decode, ~5x prefill; from Epoch AI |
| Cache read | 13 | 39 | 117 | mWh/1k tokens | Skips prefill; 1/10th of fresh input, mirrors Anthropic's 90% cache pricing discount |
| Cache write | 163 | 490 | 1,470 | mWh/1k tokens | Prefill + write overhead; 1.25x fresh input, mirrors Anthropic's 25% cache pricing surcharge |

Low = center/3, High = center×3. This gives a ~10x range from low to high.

### 2.3 What the estimates capture

**Included:** GPU/accelerator compute energy for token processing.

**Not included:**
- Full-stack datacenter overhead (PUE, cooling, networking, CPU/RAM, storage, idle power). Google reported a 2.4x multiplier from chip-active to full-stack for Gemini queries.
- Training energy (amortized across all users).
- Embodied energy (hardware manufacturing).
- User-side energy (laptop, display).
- Reasoning/thinking overhead beyond what's captured in token counts (see Section 3).

### 2.4 Key assumptions

1. **Pricing ratios approximate energy ratios.** This is the weakest assumption. Pricing reflects margin, competitive positioning, and demand management — not just energy cost. However, energy is a major cost driver for inference, so the correlation is plausible at order-of-magnitude level.

2. **GPT-4o efficiency applies to Claude.** Epoch AI measured GPT-4o. Claude Opus 4.6 is likely a different architecture and size. If Opus is larger, per-token energy is higher. This is an uncontrolled variable.

3. **±3x is the right uncertainty band.** This is a judgment call, not derived from measurement. It attempts to bracket hardware variation, model size uncertainty, batch size effects, and datacenter efficiency differences.

4. **Model-agnostic constants.** Same constants for Haiku, Sonnet, and Opus despite likely 2-5x differences in compute per token. The tool doesn't know which model is being called for subagent/intermediate API requests.

5. **Fixed per-token energy regardless of context length.** In reality, KV-cache attention cost during decode scales with context length. At 25M+ token contexts (observed in practice), decode cost per token is higher than at 4k context. The formula underestimates for long-context sessions.

### 2.5 Validation against published data

| Source | Value | Our range |
|---|---|---|
| Google median Gemini query (full-stack, Aug 2025) | 0.24 Wh | Our per-query equivalent falls within range |
| OpenAI avg ChatGPT query (Jun 2025) | 0.34 Wh | Within range |
| Couch median Claude Code session (Jan 2026) | 41 Wh | Within range |

### 2.6 Real-world measurement

A heavy day of Opus 4.6 usage (2026-02-20, 20 sessions, ~74M total tokens):

| Token type | Count | % of tokens | % of energy (center) |
|---|---|---|---|
| Fresh input | 5,122,920 | 6.9% | 21.6% |
| Output (incl. thinking) | 1,972,184 | 2.7% | 41.6% |
| Cache read | 65,092,587 | 87.9% | 27.5% |
| Cache write | 1,750,156 | 2.4% | 9.3% |

**Estimated energy: 3.1–27.6 kWh (center ~9.2 kWh)**

Equivalent to: driving an EV ~60 km, or running 3-7 refrigerators for a day, or 10,000-90,000 Google searches.

---

## 3. Token accounting validation

We built a validation harness (`analyze_tokens.py`) to determine the exact semantics of Claude Code's statusbar token fields. This was motivated by concern about potential double-counting between `total_input_tokens` and `cache_creation_input_tokens`.

### 3.1 Methodology

Set `ENERGY_DEBUG=1` to log every raw statusbar JSON payload with timestamps. The harness detects new API calls (when `total_input_tokens` increases), computes deltas, and compares against per-call `current_usage` fields.

### 3.2 Results (31 API calls across 3 concurrent sessions)

**Finding 1: `total_input_tokens` = fresh input only, excludes cache.**

Many calls show `delta(total_input) = 1` while `cache_creation_input_tokens = 992`. If total_input included cache creation, the delta would be ~993. 18/30 usable calls match `delta ≈ cu.input_tokens` (fresh only). 0/30 match `delta ≈ cu.input + cu.cache_creation`.

**Conclusion: No double-counting in the energy formula.**

**Finding 2: `total_output_tokens` includes thinking tokens.**

Finalized calls show a 1.0x ratio between `delta(total_output)` and `cu.output_tokens`. Since Anthropic's API `usage.output_tokens` includes extended thinking tokens, both counters include them. This is independently confirmed by the ~3x ratio between statusbar output totals and JSONL `message.output_tokens` (which excludes thinking) for well-covered sessions — consistent with ~60-70% thinking overhead on Opus.

**Conclusion: The energy formula already accounts for thinking tokens through the output term.**

**Finding 3: `current_usage.input_tokens` is always 1 (placeholder).**

Claude Code's statusbar never updates this field to the real per-call fresh input count. It stays at 1 throughout the call lifecycle. This field is not useful for per-call analysis, but the cumulative `total_input_tokens` is accurate.

---

## 4. JSONL log investigation

We investigated whether Claude Code's JSONL conversation logs (`~/.claude/projects/*/`) could serve as an alternative data source for token accounting.

### 4.1 Methodology

We built an independent JSONL parser (`sum_jsonl.py`, zero third-party dependencies) that reads all session and subagent JSONL files, deduplicates by `requestId` (to remove streaming duplicates), and sums token usage from assistant messages.

### 4.2 Results

**Feb 20 (heavy day, 20 sessions, 1,365 unique API requests after dedup):**

| Metric | JSONL (dedup) | Statusbar | Ratio |
|---|---|---|---|
| Input tokens | 41,444 | 7,199,162 | **174x** |
| Output tokens | 183,829 | 3,208,365 | **17x** |
| Cache read | 104,353,324 | 114,798,863 | **1.1x** |
| Cache creation | 3,170,696 | 2,717,775 | **0.9x** |

**Feb 24 (12 sessions, 1,228 unique requests):**

| Metric | JSONL (dedup) | Statusbar | Ratio |
|---|---|---|---|
| Input tokens | 11,758 | 1,193,366 | **102x** |
| Output tokens | 69,449 | 748,337 | **11x** |
| Cache read | 74,254,777 | 67,710,877 | **0.9x** |
| Cache creation | 2,817,739 | 2,003,545 | **0.7x** |

### 4.3 Interpretation

**Cache metrics match (~1x).** This is the critical cross-check. It proves both data sources are observing the same set of API calls. The discrepancy is not about missing log entries.

**Input is 100-174x off.** 75% of JSONL entries have `usage.input_tokens` of 0 or 1 — streaming placeholder values that never get updated to the final count. The JSONL also contains streaming duplicates: the same `requestId` appears 2-10 times with identical placeholder values (51-55% of all entries are duplicates).

**Output is 10-17x off.** This gap is consistent with thinking tokens being included in the statusbar's `total_output_tokens` but excluded from JSONL's `usage.output_tokens`. With ~60-70% thinking overhead on Opus, plus the placeholder effect, a 10-17x gap is expected.

**This was initially misdiagnosed.** Our first hypothesis (before building the independent parser) was "JSONL misses most API calls." A structured adversarial debate with OpenAI Codex identified the ccusage dependency as the weakest link and forced us to build independent verification. The independent parser revealed the real issue: same calls logged, but with placeholder token values.

### 4.4 Impact on third-party tools

Tools like [ccusage](https://github.com/ryoppippi/ccusage) that sum JSONL token usage will report numbers 10-174x below actual usage. This isn't a bug in those tools — the underlying JSONL data has placeholder values.

We filed this as a feature request: https://github.com/anthropics/claude-code/issues/28197

---

## 5. Summary of conclusions

| # | Conclusion | Confidence | Evidence |
|---|---|---|---|
| 1 | Energy per heavy coding day on Opus: 3-28 kWh (center ~10 kWh) | Order-of-magnitude | Derived constants, ±3x band, validated against published benchmarks |
| 2 | No double-counting in energy formula | High | 31 API calls, delta analysis, 0/30 match double-counting hypothesis |
| 3 | Thinking tokens are captured in statusbar output totals | High | 1.0x ratio to cu.output + 3x ratio vs JSONL |
| 4 | JSONL `usage.input_tokens` are streaming placeholders | High | 75% are ≤1, independent parser, two days of data |
| 5 | JSONL logs the same API calls as statusbar | High | Cache read/write match at ~1x across both days |
| 6 | Pricing-as-energy-proxy is the weakest assumption | Consensus | Agreed by both Claude and Codex in two separate debates |

---

## 6. Questions for your review

### 6.1 Do you agree with our conclusions?

Specifically:

- **Is the energy estimation methodology sound at the order-of-magnitude level?** We make no claim of precision — only that the range brackets reality. The derivation chain (Epoch AI GPT-4o measurement → Couch pricing-ratio mapping → applied to Claude Opus) has unvalidated links. Is this chain reasonable, or fundamentally flawed?

- **Is the ±3x uncertainty band appropriate?** It produces a ~10x range from low to high. Given the unknowns (model size, hardware, batch sizes, datacenter efficiency, full-stack overhead), is this too narrow, too wide, or roughly right?

- **Are the token accounting conclusions valid?** The validation harness methodology (logging statusbar payloads, computing deltas, comparing to per-call fields) — does this actually prove what we claim? Are there alternative interpretations of the data we haven't considered?

### 6.2 What are the implications of these conclusions?

- **If ~10 kWh/day is roughly right for a heavy Opus user**, what does this mean in the context of widespread AI-assisted development? A team of 10 developers could consume 100 kWh/day from AI tooling alone.

- **The JSONL placeholder issue means every community tool undercounts.** ccusage, custom dashboards, anything reading JSONL — they're all reporting 10-174x below actual usage. Is this a significant blind spot for the ecosystem?

- **Thinking tokens dominate output energy.** ~60-70% of output tokens are thinking, and output dominates energy cost (42% of total despite 2.7% of tokens). Does this have implications for how developers should think about tool usage (e.g., preferring Sonnet over Opus for simple tasks)?

### 6.3 What should we do with these conclusions?

- **Publish the findings more broadly?** The JSONL placeholder issue affects all Claude Code monitoring tools. Is a blog post warranted, or is the GitHub issue sufficient?

- **Pursue per-model energy constants?** We currently use the same constants for Haiku/Sonnet/Opus. We lack the data to differentiate (no public model sizes or energy measurements). Is it better to add speculative model multipliers, or leave the model-agnostic approach with documented limitations?

- **Advocate for better token accounting upstream?** We filed one issue. Should we push harder — e.g., requesting Anthropic publish per-model energy data, or advocating for an industry standard on inference energy reporting?

- **Anything else we should investigate or correct?**

---

## 7. Repository contents

| File | Purpose |
|---|---|
| [`statusline.py`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/statusline.py) | Main statusline script (energy monitor) |
| [`analyze_tokens.py`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/analyze_tokens.py) | Validation harness for statusbar token semantics |
| [`sum_jsonl.py`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/sum_jsonl.py) | Independent JSONL parser with deduplication |
| [`FINDINGS.md`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/FINDINGS.md) | Full investigation write-up |
| [`README.md`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/README.md) | Project documentation with methodology |
| [`energy-estimate-2026-02-20.md`](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/energy-estimate-2026-02-20.md) | Detailed energy calculation for one heavy day |

All code is Python 3 stdlib only (zero dependencies). MIT licensed.
