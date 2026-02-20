# Investigation: Token Counting Discrepancy Between Statusbar and JSONL

**Date:** 2026-02-20
**Investigators:** Magnus Gille, Claude Opus 4.6, OpenAI Codex (gpt-5.3-codex)

## Summary

We discovered that the two main data sources for Claude Code token usage — the statusbar context (`context_window.total_input_tokens`) and the JSONL conversation logs (`message.usage.input_tokens`) — report dramatically different numbers. This affects every tool that estimates Claude Code energy consumption or cost.

## The discrepancy

We compared token counts from this energy monitor (reads statusbar context) against [ccusage](https://github.com/ryoppippi/ccusage) (reads JSONL logs) for a full day of heavy Opus 4.6 usage (20 sessions, 2026-02-20).

### Daily totals

| Metric | This monitor (statusbar) | ccusage (JSONL) | Ratio |
|---|---|---|---|
| Input tokens | 7,091,039 | 152,319 | 46.6x |
| Output tokens | 3,020,307 | 169,655 | 17.8x |
| Cache read | ~76,500,000 | ~77,350,000 | ~1.0x |
| Cache write | ~1,950,000 | ~9,660,000 | 0.2x |
| Estimated cost | $446 (Opus API rates) | $50 (ccusage) | 8.9x |

Cache read tokens are roughly consistent. Input and output tokens differ by 17-47x.

### Per-session ratios vary wildly

| Session | Input ratio | Output ratio |
|---|---|---|
| Best-covered session | 2.9x | 3.0x |
| Typical session | 50-500x | 5-30x |
| Worst-covered session | 15,619x | 701x |

The ratios range from ~3x (for sessions with good JSONL coverage) to 15,000x+ (for sessions where the JSONL captured almost nothing).

## Root cause

Two compounding factors:

### 1. JSONL logs are incomplete

The JSONL conversation logs do not record all API calls. They appear to capture top-level conversation turns but miss:

- Tool use intermediate calls (file reads, code execution, etc.)
- Subagent/Task API calls
- Context management operations (auto-compact, etc.)
- Retry and streaming intermediate calls

Evidence: some sessions have only 49 JSONL input tokens vs 765,323 statusbar input tokens (15,619x ratio), while others are within 3x. The coverage is inconsistent and unpredictable.

### 2. Thinking tokens appear in statusbar but not JSONL

For sessions with good JSONL coverage (~3x ratio), the remaining gap on the output side is consistent with Opus's extended thinking tokens being included in the statusbar's `total_output_tokens` but excluded from the JSONL's per-message `output_tokens`.

A ~3x output multiplier means roughly 60-70% of output tokens are thinking tokens — plausible for Opus.

## What this means

### For energy estimation

The statusbar's `total_input_tokens` and `total_output_tokens` are the more complete measure of actual compute work. The JSONL captures only a fraction. Any energy or cost estimate based solely on JSONL data (including ccusage) will significantly undercount.

However, we don't yet know the exact semantics of `total_input_tokens`. It may include cache creation tokens (which we also count separately), leading to potential double-counting in the energy formula. This needs verification.

### For ccusage and similar tools

ccusage reported $50 for a day where the statusbar-based estimate was $446 at API rates. This 9x gap means power users looking at ccusage may think they're consuming far less compute than they actually are.

This isn't a bug in ccusage — it correctly sums what's in the JSONL. The JSONL itself is an incomplete audit log, not a complete accounting of API usage.

### For understanding real-world energy impact

A full workday of Claude Code on Opus 4.6 produced:
- **Statusbar totals:** 7.1M input + 3.0M output + 76.5M cached + 1.9M cache write
- **Energy estimate (center):** ~10 kWh (range: 3.1-27.6 kWh)
- **Everyday comparison:** equivalent to driving an EV 60 km, or running 3-7 refrigerators for a day

This is not extreme or unusual usage — it's a developer using Claude Code as their primary tool for a workday, across ~12 working sessions. The energy cost is structural, driven by:
- Output token generation (43% of energy, dominated by thinking tokens)
- Cache reads (27% of energy, from Claude Code's context-resend architecture)
- Fresh input processing (21% of energy)

## Adversarial review (debate with Codex)

The energy estimates were stress-tested through a structured 2-round adversarial debate between Claude Opus 4.6 and OpenAI Codex (gpt-5.3-codex). Full debate transcript is in `debate/` (gitignored as process artifact).

### Points of agreement

1. The estimate is a useful **order-of-magnitude indicator**, not a calibrated measurement
2. The derivation chain (Epoch AI GPT-4o analysis -> Couch pricing-ratio mapping -> applied to Opus) is a "proxy stack" with unvalidated links
3. The ±3x uncertainty band is a minimum; the true value could exceed the high estimate
4. Context-length decode scaling (fixed per-token energy ignoring context size) is a blind spot
5. The weakest assumption is using pricing ratios as an energy proxy, especially for cache operations

### Points of disagreement

1. Whether thinking tokens are captured (now resolved: they are, in the statusbar)
2. Whether the `total_input_tokens` monotonicity assumption is safe (untested)
3. Specific numeric adjustments for model size, context scaling, etc. (ungrounded)

### Codex's recommended next step

Build a token-accounting validation harness that logs raw statusbar payloads per-update alongside JSONL entries, to definitively resolve what each field contains.

## Open questions

1. **What exactly does `total_input_tokens` include?** Does it overlap with cache creation tokens? If so, the energy formula double-counts.
2. **Does `total_output_tokens` include thinking tokens?** The ~3x ratio in well-covered sessions suggests yes, but not definitively proven.
3. **Why is JSONL coverage so inconsistent?** Some sessions have ~3x ratios, others 15,000x. What determines which API calls get logged?
4. **Should energy constants differ for thinking vs. visible output?** Thinking may be batched differently or use different hardware paths.

## Next steps

1. **Build validation harness:** Log raw statusbar context alongside JSONL entries to resolve the semantics question definitively
2. **Fix potential double-counting:** Once we know what `total_input_tokens` includes, adjust the energy formula
3. **Publish finding:** The JSONL incompleteness affects every CC monitoring tool — this should be shared with the community
4. **Update README:** Revise the "heavy day" range estimate and add caveats about token counting methodology

## Data

All raw data from this investigation is available in:
- `~/.claude/statusline_daily.json` (today's statusbar-based totals)
- Claude Code's JSONL logs at `~/.claude/projects/`
- `debate/` directory (adversarial review transcript, gitignored)
