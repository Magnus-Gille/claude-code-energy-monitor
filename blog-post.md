# Claude Code's JSONL Logs Undercount Tokens by 100x — Here's Why

Every tool that reads Claude Code's JSONL conversation logs for token accounting is working with bad data. The `usage.input_tokens` field is a streaming placeholder — 75% of entries are 0 or 1, never updated to the real value. The result: input tokens undercounted by 100–174x, output tokens by 10–17x.

I discovered this while building an [energy monitor](https://github.com/Magnus-Gille/claude-code-energy-monitor) for Claude Code. The monitor reads the statusbar's `context_window` totals (the accurate source), but I wanted to cross-check against JSONL to validate my numbers. What I found instead was that the JSONL data is essentially unusable for token accounting — and every community tool that relies on it, including [ccusage](https://github.com/ryoppippi/ccusage), is affected.

I've [filed this as a feature request](https://github.com/anthropics/claude-code/issues/28197) with Anthropic. Here's the full investigation.

## The data

I compared JSONL token sums (deduplicated by `requestId`) against statusbar cumulative totals across two full days of Opus 4.6 usage.

**Feb 20 — heavy day, 20 sessions, 1,365 unique API requests:**

| Metric | JSONL (dedup) | Statusbar | Ratio |
|---|---|---|---|
| Input tokens | 41,444 | 7,199,162 | **174x** |
| Output tokens | 183,829 | 3,208,365 | **17x** |
| Cache read | 104,353,324 | 114,798,863 | **1.1x** |
| Cache creation | 3,170,696 | 2,717,775 | **0.9x** |

**Feb 24 — moderate day, 12 sessions, 1,228 unique requests:**

| Metric | JSONL (dedup) | Statusbar | Ratio |
|---|---|---|---|
| Input tokens | 11,758 | 1,193,366 | **102x** |
| Output tokens | 69,449 | 748,337 | **11x** |
| Cache read | 74,254,777 | 67,710,877 | **0.9x** |
| Cache creation | 2,817,739 | 2,003,545 | **0.7x** |

Look at the cache columns. Cache read and cache creation match at roughly 1x across both days. This is the critical cross-check: **both data sources are observing the same set of API calls.** The discrepancy isn't about missing log entries — it's about what values are recorded for each entry.

Input tokens are 100–174x off. Output tokens are 10–17x off. But cache metrics are fine.

### The smoking gun: 75% placeholders

I built an [independent JSONL parser](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/sum_jsonl.py) (zero dependencies, ~200 lines of Python) to inspect the raw data. The distribution of `usage.input_tokens` values tells the story:

**75% of all JSONL entries have `usage.input_tokens` of 0 or 1.** These are streaming placeholder values. Claude Code writes JSONL entries during streaming, when the input token count hasn't been finalized yet. The placeholder is never updated after the request completes.

The JSONL also contains streaming duplicates: the same `requestId` appears 2–10 times with identical placeholder values. Across the two days I analyzed, 51–55% of all entries were duplicates.

## The misdiagnosis

I didn't arrive at the right answer on my first try. My initial hypothesis was wrong, and it took an adversarial review to break me out of it.

When I first compared ccusage's numbers against my statusbar-based monitor, I saw per-session ratios varying wildly — from 3x to 15,000x:

| Session type | Input ratio | Output ratio |
|---|---|---|
| Best-covered session | 2.9x | 3.0x |
| Typical session | 50–500x | 5–30x |
| Worst-covered session | 15,619x | 701x |

This pattern — some sessions close, others off by four orders of magnitude — looked exactly like missing log entries. My first hypothesis: "JSONL misses most API calls. It captures top-level conversation turns but skips tool use, subagents, context management, and retries."

It seemed plausible. Claude Code uses tools heavily (file reads, code execution, subagent tasks), and each tool interaction triggers API calls that might not be logged. The wild per-session variation could reflect sessions with more or fewer tool calls.

I was wrong.

### What broke the hypothesis

I had been running an adversarial debate — Claude Opus vs OpenAI Codex, structured critique of the energy monitor's methodology. Codex identified the weakest link in my analysis: I was using ccusage (a third-party tool with its own parsing logic) as my JSONL baseline. Any discrepancy could be in ccusage's parser, in the JSONL format, or in my statusbar readings. I couldn't distinguish between them.

Codex's recommendation: build independent verification. Don't trust ccusage's numbers — parse the JSONL yourself.

So I built `sum_jsonl.py`: a standalone parser that reads every JSONL file, deduplicates by `requestId`, and sums raw `usage` fields directly. No dependencies, no interpretation, just arithmetic on the raw data.

The independent parser confirmed the discrepancy — but the cache metrics told a different story. Cache read and cache creation matched at ~1x between JSONL and statusbar. If JSONL were missing API calls, cache metrics would be off too. They weren't. **The same calls were logged in both places, but with different token values.**

The misdiagnosis collapsed. The issue wasn't missing entries — it was placeholder values that never get updated.

## Root cause: streaming architecture

Claude Code's JSONL logging happens during streaming, not after request completion. Here's the sequence:

1. **Request starts** → JSONL entry created with `usage.input_tokens = 1` (placeholder)
2. **Tokens stream in** → Additional JSONL entries for the same `requestId`, with `usage.output_tokens` incrementing
3. **Request completes** → `usage.input_tokens` is never updated to the final value

Cache fields (`cache_read_input_tokens`, `cache_creation_input_tokens`) appear to be set correctly from the start — they're available in the initial API response before streaming begins, which explains why they match at ~1x while input tokens don't.

The streaming duplicates compound the problem. A single request generates 2–10 JSONL entries with the same `requestId`. Any parser that doesn't deduplicate will multiply the placeholder errors.

### Which fields are reliable vs. placeholders

| JSONL field | Status | Notes |
|---|---|---|
| `usage.input_tokens` | **Placeholder** | Usually 0 or 1. Never updated. |
| `usage.output_tokens` | **Partial** | Real but excludes thinking tokens. |
| `usage.cache_read_input_tokens` | Accurate | Matches statusbar at ~1x. |
| `usage.cache_creation_input_tokens` | Accurate | Matches statusbar at ~1x. |

## The output gap: thinking tokens

Even if `usage.input_tokens` were fixed, output tokens would still be 10–17x off. That's because JSONL's `usage.output_tokens` excludes extended thinking (chain-of-thought) tokens, while the statusbar's `total_output_tokens` includes them.

Anthropic's [adaptive thinking documentation](https://docs.anthropic.com/en/docs/build-with-claude/adaptive-thinking) classifies thinking as output tokens for billing: "Tokens used during thinking (output tokens)." There is no separate `thinking_tokens` field in the API `usage` object — thinking is counted within `output_tokens` at the API level.

But JSONL records only the visible output. For Opus, where roughly 60–70% of output tokens are thinking, this means JSONL captures less than a third of actual output. Combined with the input placeholder issue, the JSONL gives you a fraction of a fraction.

I confirmed this with a [validation harness](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/analyze_tokens.py) that logged raw statusbar payloads across 31 API calls: the statusbar's `total_output_tokens` matched the API's `usage.output_tokens` at a 1.0x ratio — both include thinking. JSONL just records something different.

## Impact

**ccusage reported $50 for a day that was actually $446 at API rates.** That's an 8.9x gap. Any JSONL-based monitoring tool will underreport by 1–2 orders of magnitude.

This isn't a bug in ccusage or any other tool — they correctly sum what's in the JSONL. The data they're reading is the problem. If you're making decisions about which model to use, how to optimize your workflow, or how much compute your team is consuming based on JSONL-derived numbers, those decisions are based on data that's 100x too low on input and 10x too low on output.

## The fix

What Anthropic could do:

1. **Write final `usage` values.** After a request completes, update the JSONL entry — or append a final entry — with the real `input_tokens` and `output_tokens` (including thinking).

2. **Deduplicate streaming entries.** Either log only the final state per request, or add a `"final": true` marker so parsers know which entry to use.

A simpler alternative: a separate append-only usage log with one line per completed request:

```jsonl
{"ts":1740000000,"requestId":"req_...","model":"opus","input_tokens":1234,"output_tokens":567,"cache_read":50000,"cache_creation":800}
```

**Current workaround:** Read the statusbar's `context_window` totals via a custom [statusline script](https://github.com/Magnus-Gille/claude-code-energy-monitor). This gives accurate session-level aggregates, but not per-call breakdowns.

## How we verified this

The conclusions rest on three independent lines of evidence:

1. **Independent JSONL parser** ([sum_jsonl.py](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/sum_jsonl.py)): Zero dependencies, reads raw JSONL, deduplicates by `requestId`. Reproduced the same pattern across two full days of data.

2. **Validation harness** ([analyze_tokens.py](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/analyze_tokens.py)): Logged every raw statusbar JSON payload across 31 API calls in 3 concurrent sessions. Confirmed `total_input_tokens` excludes cache (no double-counting), `total_output_tokens` includes thinking (1.0x ratio to API), and `current_usage.input_tokens` is always 1 (placeholder).

3. **API billing reconciliation** ([FINDINGS.md](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/FINDINGS.md)): 4 direct Anthropic API calls with a personal key. All four token categories (fresh input, output, cache read, cache write) matched the billing dashboard CSV to the exact token. Cost: $0.01.

The full evidence chain is documented in [FINDINGS.md](https://github.com/Magnus-Gille/claude-code-energy-monitor/blob/master/FINDINGS.md). All tools are in the [repo](https://github.com/Magnus-Gille/claude-code-energy-monitor) — MIT licensed, stdlib-only Python.

---

*This finding came out of building a [real-time energy monitor](https://github.com/Magnus-Gille/claude-code-energy-monitor) for Claude Code. The monitor reads statusbar totals (the accurate source) and estimates compute energy per session and per day. If you're curious about how much energy your AI-assisted coding actually consumes, check it out.*

*Filed as [GitHub issue #28197](https://github.com/anthropics/claude-code/issues/28197).*
