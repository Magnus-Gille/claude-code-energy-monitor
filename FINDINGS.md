# Investigation: Token Counting Discrepancy Between Statusbar and JSONL

**Date:** 2026-02-20
**Investigators:** Magnus Gille, Claude Opus 4.6, OpenAI Codex (gpt-5.3-codex)

## Summary

We discovered that the two main data sources for Claude Code token usage — the statusbar context (`context_window.total_input_tokens`) and the JSONL conversation logs (`message.usage.input_tokens`) — report dramatically different numbers. This affects every tool that estimates Claude Code energy consumption or cost.

The measurements in the original discrepancy investigation are historical observations from
February 20–24, 2026, on the Claude Code builds available then. They remain useful evidence about
those captures, but they do not establish universal behavior across Claude Code versions or
workloads.

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

### 1. JSONL `usage` fields were streaming placeholders in the February 2026 captures

The JSONL logs record streaming snapshots of the `usage` object. In those captures, most fields
were never updated to their final values:

- **`input_tokens`:** A placeholder (`1` or `3`) in the February sample. Across 69 multi-entry requestIds examined, `input_tokens` never changed between the first and last streaming entry (100%). Single-entry requestIds were also placeholder (93%).
- **`output_tokens`:** Partially eventually consistent in that sample. In 35% of multi-entry requests, `output_tokens` grew across streaming entries (e.g. `[1, 1, 211]` or `[4, 3182]`), and the final entry reflected the visible output count. The sample also did not expose thinking in this field.
- **`cache_read_input_tokens`:** Stable and correct from the first entry. Matches statusline within 0.8-1.4x across all dates tested.
- **`cache_creation_input_tokens`:** Also stable and correct from the first entry.

| JSONL usage field | Eventually consistent? | Usable for accounting? |
|---|---|---|
| `input_tokens` | No in the February sample (placeholder) | No for that sample |
| `output_tokens` | Partially in the February sample (visible output only) | No for that sample — 6-98x gap |
| `cache_read_input_tokens` | Yes (stable from first entry) | Yes |
| `cache_creation_input_tokens` | Yes (stable from first entry) | Yes |

Even with the February sample's "last entry wins" deduplication, JSONL output totals remain 6-98x
lower than statusline. Later collection uses `requestId` deduplication with the maximum of each
token field, which preserves a completed value when an early streamed record is only a placeholder:

| Date | JSONL output (deduped) | Statusline output | Ratio |
|---|---|---|---|
| 2026-02-20 | 182,292 | 3,208,365 | 17.6x |
| 2026-02-23 | 158,532 | 4,157,122 | 26.2x |
| 2026-02-24 | 98,856 | 1,112,591 | 11.3x |

Note: our earlier claim that "JSONL misses API calls" was wrong for cache metrics in the February
sample — the ~1x cache read ratio showed both sources tracking the same observed API calls. The
real issue in that sample was that per-call `input_tokens` and `output_tokens` in the JSONL were
streaming artifacts, not finalized values.

### 2. Thinking-token visibility in the February 2026 sample

For sessions with good JSONL coverage (a roughly 3x ratio), the remaining gap on the output side
was consistent with Opus's extended thinking tokens being included in the statusbar's
`total_output_tokens` but excluded from the JSONL per-message `output_tokens`. This interpretation
is specific to the February 2026 captures.

Anthropic's [adaptive thinking docs](https://docs.anthropic.com/en/docs/build-with-claude/adaptive-thinking) confirm that thinking tokens are classified as output tokens for billing: "Tokens used during thinking (output tokens)." In the February captures, the JSONL `usage` object had no separate thinking field; the interpretation above concerns those captures only. Later versions must be checked independently.

A roughly 3x output ratio in those captures implied that roughly 60–70% of output tokens were
thinking tokens, which was plausible for Opus. It is not a universal multiplier.

## What this means

### For energy estimation

In the February 2026 captures, the statusbar's `total_input_tokens` and `total_output_tokens`
covered more of the observed work than the JSONL fields. That does not make statusline data
universally complete: later v2.1.226 checks found statusline collection missed subagent and
non-interactive calls. Transcript and statusline coverage must be evaluated for the version and
workload being measured.

However, we don't yet know the exact semantics of `total_input_tokens`. It may include cache creation tokens (which we also count separately), leading to potential double-counting in the energy formula. This needs verification.

### For ccusage and similar tools

In the February 2026 sample, ccusage reported $50 for a day where the statusbar-based estimate was
$446 at API rates. That dated 9x gap does not establish a current or universal relationship.

This isn't a bug in ccusage — it correctly sums what's in the JSONL. The comparison was a local
source-coverage check, not API billing reconciliation, and neither local source is complete for
every version or workload.

### For understanding real-world energy impact

A full workday of Claude Code on Opus 4.6 produced:
- **Statusbar totals:** 7.1M input + 3.0M output + 76.5M cached + 1.9M cache write
- **Energy estimate (mid, revised constants):** ~9 kWh (snaps to ~10 kWh in the order-of-magnitude display)
- **Everyday comparison:** roughly equivalent to running a fridge for ~4–5 days

This is not extreme or unusual usage — it's a developer using Claude Code as their primary tool for a workday, across ~12 working sessions. With the revised constants (Feb 2026, see below), the energy cost on this *interactive* day was driven by:
- Output token generation (~46% of energy, dominated by thinking tokens)
- Fresh input processing (~31% of energy)
- Cache reads (~13% of energy, reduced from 27% after physics-derived cache discount)
- Cache write (~10% of energy)

Note: The original constants (Couch 2026, pricing-derived) estimated ~13 kWh center for this day. The revised constants reduce this to ~9 kWh, primarily due to the cache read discount changing from 10x to 26x and output from 1,950 to 1,400 mWh/1k tokens.

> **2026-05 caveat:** the 7.1M "input" figure above was collected with the pre-v2.1.122 statusline semantics. A later audit found that fresh-input counting became inflated after CC v2.1.122 and that, in heavily-cached workloads, truly-fresh input is small (most prefill work is cache creation). See the audit update at the end of this file. The output-dominated breakdown above is also specific to long *interactive* sessions; headless/automated workloads are cache-write-dominated.

## Adversarial review (debate with Codex)

### Debate 1: Energy estimate validity (Feb 2026)

The energy estimates were stress-tested through a structured 2-round adversarial debate between Claude Opus 4.6 and OpenAI Codex (gpt-5.3-codex). Full debate transcript is in `debate/` (gitignored as process artifact).

**Points of agreement:**
1. The estimate is a useful **order-of-magnitude indicator**, not a calibrated measurement
2. The derivation chain (Epoch AI GPT-4o analysis -> Couch pricing-ratio mapping -> applied to Opus) is a "proxy stack" with unvalidated links
3. The ±3x uncertainty band is a minimum; the true value could exceed the high estimate
4. Context-length decode scaling (fixed per-token energy ignoring context size) is a blind spot
5. The weakest assumption is using pricing ratios as an energy proxy, especially for cache operations

**Codex's recommended next step:** Build a token-accounting validation harness — DONE (see below).

### Debate 2: Energy constants revision (Feb 2026)

A second debate focused on whether pricing-derived ratios should be replaced with physics-derived values. See `debate/energy-constants-summary.md`.

**Key outcomes:**
1. **Output constant reduced** from 1,950 to 1,400 mWh/1k tokens. Multiple cross-checks (FLOP-based, AI Energy Score, Llama 405B measurements) cluster at 600–1,800, making 1,950 too high.
2. **Cache read constant reduced** from 39 to 15 mWh/1k tokens (~26x discount vs input, up from 10x). Physics shows cache reads skip all prefill computation — the energy is primarily KV cache loading from memory. The 10x pricing discount reflected business strategy and storage amortization, not compute energy.
3. **Fresh input and cache write unchanged.** 390 mWh/1k is well-anchored to Epoch AI's long-context analysis, which matches Claude Code's typical 50–200k context windows.
4. **Display changed** from precise lo–hi range to order-of-magnitude (e.g. `~5kWh`), reflecting genuine uncertainty.

**Impact:** Monthly estimate for a heavy user dropped from ~73 to ~48 kWh (mid).

## Validation harness results (2026-02-24, Claude Code build then current)

We built a validation harness (`analyze_tokens.py`) that logs every raw statusbar payload and analyzes the relationship between cumulative totals and per-call `current_usage` fields.

### Q1: Does `total_input_tokens` include `cache_creation_input_tokens`?

**NO for that build. No double-counting.** Confirmed across 31 API calls in 3 concurrent sessions.

Evidence:
- Many calls show `delta(total_input) = 1` while `cache_creation = 992`, `655`, `287`, etc. If total_input included cache creation, those deltas would be ~1000, not 1.
- 18/30 usable calls have delta matching `cu.input_tokens` exactly (fresh only). 0/30 match `cu.input + cu.cache_creation` (the double-counting hypothesis).
- 10 "neither" calls are explained by `cu.input_tokens` being stale at call start (shows previous call's value, typically 1).

For that build, `total_input_tokens` = cumulative fresh input tokens only. Cache creation and cache
read are separate.

### Q2: Did `total_output_tokens` include thinking tokens in the February 2026 run?

**YES for that run.** Supported by three independent lines of evidence:

1. **Anthropic docs classify thinking as output tokens.** The [adaptive thinking documentation](https://docs.anthropic.com/en/docs/build-with-claude/adaptive-thinking) states under Pricing that thinking incurs charges for "Tokens used during thinking (output tokens)" and that "Output tokens (billed): The original thinking tokens that Claude generated internally." The February capture had no separate thinking field. This supported that run's interpretation; it does not establish how every later Claude Code surface serializes thinking.
2. **Finalized calls in that run showed a 1.0x ratio** between `delta(total_output)` and `cu.output_tokens`. Since the API's `usage.output_tokens` includes thinking (per the docs above), both counters included it in that run.
3. **The initial 3x ratio was a dated comparison:** JSONL's `message.output_tokens` did not expose thinking in that sample, giving a roughly 3x gap. This implied roughly 60–70% of output tokens were thinking for that workload.

Note: The docs confirm thinking tokens are billed as output tokens and counted within `output_tokens`. However, there is no single sentence stating "`usage.output_tokens` includes thinking tokens" — this is inferred from (a) thinking being classified as output tokens, (b) no separate usage field existing for thinking, and (c) our empirical 1.0x ratio in point 2.

### Bonus: `current_usage.input_tokens` was always 1 in the February 2026 run

In that run, Claude Code's statusbar never updated `cu.input_tokens` to the real per-call fresh
input count — it stayed at 1 (placeholder). It was not useful for per-call analysis, but
`total_input_tokens` was accurate for cumulative tracking under that build's semantics.

### What `current_usage` contains

Four fields observed in the February run: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`. Values updated during streaming (output grew incrementally) but
`input_tokens` appeared stuck at 1.

### Impact on energy formula

**No changes needed.** The current formula correctly treats `total_input_tokens` as fresh-only and adds cache creation separately. The energy estimate is as accurate as the underlying constants allow.

## API billing reconciliation (2026-02-24)

We made 4 direct Anthropic API calls (`api_test.py`) with a personal API key and compared the token counts from each API response's `usage` object against the Anthropic usage dashboard CSV export.

### Test design

| Test | Purpose | Expected behavior |
|---|---|---|
| 1. Short prompt | Minimal tokens, baseline | Fresh input + output only |
| 2. Longer output | More output tokens | Higher output count |
| 3. Cache write | Large system prompt, first call | `cache_creation_input_tokens` populated |
| 4. Cache read | Same system prompt, second call | `cache_read_input_tokens` populated |

Model: `claude-sonnet-4-20250514` (to keep costs low).

### Results

| Token type | API response totals | Dashboard CSV | Match? |
|---|---|---|---|
| Fresh input | 65 | 65 | exact |
| Output | 249 | 249 | exact |
| Cache read | 1,402 | 1,402 | exact |
| Cache write (5min) | 1,402 | 1,402 | exact |

**Total cost: $0.0096 (~1 cent)**

### What this confirms

1. **The API `usage` object is the ground truth** — the exact same numbers appear on the billing dashboard.
2. **Our energy formula uses the right inputs.** The four token categories we track (fresh input, output, cache read, cache write) are the same four the API bills for. No hidden token types, no misattributed categories.
3. **Cache semantics are correct.** Cache write shows up on the first call, cache read on the second — exactly as our `statusline.py` accumulation logic expects.
4. **No token inflation or deflation in this API test.** What the API reported was what got billed.
   This direct API result does not establish transcript or statusline source coverage.

### Relationship to statusbar validation

This closes the loop on the full data path:

```
API response (usage object)  ──exact match──▶  Billing dashboard
        │
        ▼
Statusbar (context_window)   ──1:1 ratio──▶  Our energy formula
```

The earlier validation harness checked statusbar field semantics for the tested February 2026
build, while this separate API test confirmed that the API `usage` object matched the billing
dashboard for those four calls. These tests do not reconcile transcript coverage with billing and
do not prove that either local source is complete for every version or workload.

## Later source-coverage re-validation — 2026-09-03 (Claude Code v2.1.226)

A later comparison changed how the older discrepancy should be described:

- Streaming transcript records were deduplicated by `requestId`, taking the maximum of each token
  field because an early content-block record can contain a placeholder while a later record has
  the completed value.
- For a full observed calendar day, deduplicated transcript output was within 1% of statusline
  output. This is evidence about source coverage for that version and day, not a universal output
  multiplier and not an API billing reconciliation.
- The statusline collector observes main-thread status updates and misses subagent and
  non-interactive (`entrypoint: sdk-cli`) calls. Transcript collection can therefore cover calls
  that statusline collection does not.

The statusline and transcripts are complementary sources. Coverage claims must name the Claude
Code version, date, workload, and deduplication rule used for the comparison.

## Remaining open questions

1. ~~**What exactly does `total_input_tokens` include?**~~ **RESOLVED for the February build:** Fresh input only, excludes cache. The May update documents changed semantics.
2. ~~**Does `total_output_tokens` include thinking tokens?**~~ **RESOLVED:** Yes.
3. ~~**Why was JSONL coverage so inconsistent in February 2026?**~~ **RESOLVED for that sample:** JSONL cache metrics matched ~1x, while `usage.input_tokens` was a streaming placeholder and `usage.output_tokens` did not expose thinking. Later v2.1.226 evidence found transcript output within 1% of statusline output for a full observed day after requestId deduplication, while statusline missed subagent and non-interactive calls.
4. **Should energy constants differ for thinking vs. visible output?** Thinking may be batched differently or use different hardware paths.
5. **Model-size energy scaling:** Same constants for Haiku/Sonnet/Opus despite likely 2-5x differences in actual compute.

## Next steps

1. ~~**Build validation harness**~~ **DONE** (`analyze_tokens.py`, enabled via `ENERGY_DEBUG=1`)
2. ~~**Fix potential double-counting**~~ **NOT NEEDED** — no double-counting found
3. **Publish finding:** Share the dated February 2026 JSONL observations, with version and workload
   labels, so current source coverage is not inferred from them.
4. **Update README:** Revise the "heavy day" range estimate and add caveats about token counting methodology
5. **Consider per-model energy constants:** Different multipliers for Haiku/Sonnet/Opus based on estimated model sizes
6. ~~**Billing reconciliation test**~~ **DONE** (`api_test.py`). 4 API calls, all 4 token categories matched dashboard CSV exactly. Cost: $0.01.
7. **Reframe energy output:** Per expert review, reframe from "energy estimate" to "compute-energy proxy (token-work scaled)" with optional full-stack datacenter multiplier. Label ±3× as minimum plausible range, not statistical bracket.

## Interpretation Notes (boundaries & semantics)

The counter semantics below describe the February and May 2026 validation runs. They are
version-specific observations and should not be read as guarantees for later Claude Code builds.

### Boundary definition
The energy estimate in this project is scoped to "token processing compute" and should be interpreted as:
> a *work-proportional proxy* for accelerator energy attributable to prefill/decode/cache operations.

It is not a direct measurement of wall-plug electricity, and it does not define an attribution method for shared infrastructure.

### Semantics we rely on
The February validation relied on monotonic cumulative counters because per-call `current_usage`
fields were partially placeholders. That build's totals were derived from deltas of:
- `total_input_tokens` (fresh/non-cached input)
- `total_output_tokens` (included thinking tokens in that run)
- cache read / cache creation counters (as reported by statusline)

In the February 2026 captures, JSONL `usage.input_tokens` was a placeholder, `usage.output_tokens`
did not expose thinking, and cache metrics were the reliable fields. That observation does not
apply universally: v2.1.226 transcript output matched statusline output within 1% for a full
observed day after requestId deduplication, while statusline missed subagent and non-interactive
calls. See "Root cause" and "Later source-coverage re-validation" above.

### Reproducibility
Token semantics are validated by capturing raw statusline payloads (`ENERGY_DEBUG=1`) and comparing deltas across call boundaries. This reduces the risk of:
- double-counting cache creation inside input totals
- missing thinking tokens in output totals

### What would change our conclusions
The token accounting conclusions would need revisiting if:
- statusline totals are not monotonic within a session
- Claude Code changes which counters are placeholders vs finalized
- the statusline payload schema changes (field renames/redefinitions)

The energy conclusions would materially change if:
- a provider publishes measured per-model Wh/token with stated boundaries
- we obtain direct measurement of token-level energy on known hardware (rather than proxy scaling)

## Data

All raw data from this investigation is available in:
- `~/.claude/statusline_daily.json` (today's statusbar-based totals)
- Claude Code's JSONL logs at `~/.claude/projects/`
- `debate/` directory (adversarial review transcript, gitignored)

## Audit update — 2026-05-30 (CC v2.1.157, Opus 4.8)

A re-validation found that the Feb 2026 token-semantics conclusions had gone stale, because **Claude Code v2.1.122 redefined two statusbar fields**.

### What changed in the payload

Captured raw payloads (ENERGY_DEBUG=1 on v2.1.157) show:

```
total_input_tokens : 223213
current_usage: { input_tokens: 2, cache_creation_input_tokens: 3730, cache_read_input_tokens: 219481 }
                  →  2 + 3730 + 219481 = 223213   (exact)
```

- `total_input_tokens` is now `current_usage.input_tokens + cache_creation + cache_read` of the **most recent response** — the current-context total, **not** the cumulative fresh-input counter validated in Feb 2026.
- `total_output_tokens` equals `current_usage.output_tokens` for the most recent response (per-call; resets each call), **not** a cumulative session total.
- `current_usage.input_tokens` is a near-placeholder (1–2); genuine fresh input is `total_input − cache_read − cache_creation`.
- New top-level fields are present: `rate_limits.{five_hour,seven_day}.used_percentage`, `effort`, `thinking`, `fast_mode`.

### Impact on the monitor (now fixed)

The old `update_daily` accumulated `max(0, total_input − prev)` and `max(0, total_output − prev)`, which assumed monotonic cumulative counters. Replaying 392 captured fires against independently-computed ground truth:

| Token type | OLD logic | TRUTH | Error |
|---|---|---|---|
| fresh input | 721,457 | 13,519 | **~53× over-count** (was tracking context growth) |
| output | 105,966 | 111,293 | under-count |
| cache_read | 16,942,573 | 16,942,573 | exact (unaffected) |
| cache_write | 171,595 | 171,595 | exact (unaffected) |

The fix accumulates per-call `current_usage` fields, detecting call boundaries (input-side signature change **or** output reset — the latter also fixes a pre-existing miss on consecutive identical fully-cached calls) and summing each call once. The corrected logic was replay-validated to match ground truth exactly on all four token types. Net effect: the fresh-input energy share falls from a spurious ~12–15% to ~1% (real prefill work is correctly attributed to cache creation); output is no longer under-counted; cache terms are unchanged.

Full constant-level discussion, new 2026 energy literature, and per-model multipliers are in [docs/energy-constants.md](docs/energy-constants.md#2026-05-30-audit-update).

## Claude subagent transcripts lack the final usage row — 2026-09-29

Claude Code subagent transcripts often never contain the final usage row of a request: only the stream-start snapshot is written, with `message.stop_reason: null` and `output_tokens` of a few tokens even for long outputs. On one machine, 1,537 of 3,125 subagent requests and 209 of 219 workflow-agent requests had no row with a `stop_reason`, against 0 of 3,256 main-thread requests. The share varies by Claude Code version: 2.1.270 had 15 of 1,792 requests without a final row, 2.1.282 had 791 of 1,798, and 2.1.284 had 406 of 706.

History now records `output_final` per Claude request. When no row carries a `stop_reason`, the observation gets the warning `output_not_final`, is incomplete, and its output and reasoning confidence is `lower_bound`. Recovering the true subagent output from another source is future work.
