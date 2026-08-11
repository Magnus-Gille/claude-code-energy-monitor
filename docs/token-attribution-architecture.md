# Usage Attribution Architecture

**Status:** proposal, not yet implemented
**Date:** 2026-08-10
**Scope:** answering "where did my tokens go?" across arbitrary machines, harnesses, and providers

---

## 1. The problem

The monitor today answers *how much*. It cannot answer *what caused it*.

A user on a metered subscription burns half a week's allowance in thirty minutes while several
agents run in parallel. The questions they immediately have are:

1. Was that me, or something running that I forgot about?
2. Which session, which agent, which project, which model?
3. Did an agent get stuck in a loop?
4. Did I leave an expensive setting on — a bigger model, a higher effort level, fast mode?
5. Is a *different machine* draining the same account?
6. What would I change to make it cheaper — smaller model, lower effort, fewer agents,
   shorter sessions?

None of these are answerable from a daily token total. They need **attribution**: every unit of
consumption tied to the thing that caused it, along dimensions the user can actually act on.

This document specifies that system. It is deliberately harness-agnostic and provider-agnostic:
the same machinery must work for Claude Code, Codex CLI, the Pi harness and future harnesses,
against Anthropic (subscription or API), OpenAI, OpenRouter, or a locally hosted model — in any
combination, including several at once on the same machine.

Two of these are answerable well, and the design should not pretend otherwise: §8.4 records what this
system structurally *cannot* tell you, most importantly that it reports savings in tokens rather than
in percent of your allowance.

### 1.1 Explicit non-goals

- Not a billing system. Provider invoices remain authoritative for money.
- Not a replacement for a harness's own in-app usage view. Claude Code's `/usage`, for example,
  already shows a local attribution breakdown; it is explicitly single-machine, current-window,
  and not exportable. This system is for the cross-machine, cross-harness, historical, scriptable
  case — which is precisely what the built-in views do not cover.
- Not a cross-provider energy benchmark. The repository's energy constants are Anthropic-anchored
  order-of-magnitude proxies and stay that way (see `docs/energy-constants.md`).
- No always-on daemon in the default configuration.

---

## 2. Evidence: why the current design cannot answer the question

Measured on one machine, snapshot taken 2026-08-10, comparing the monitor's own accumulated totals
against a deduplicated sum of every API call in the locally available transcripts. Transcript
retention is seven days; three carried activity.

| Day | Workload | Monitor output | Actual output | Coverage | Cache-read coverage |
|---|---|---:|---:|---:|---:|
| 2026-08-08 | one interactive session, no subagents | 24,900 | 24,900 | **100%** | **100%** |
| 2026-08-10 | parallel agents + headless runs | 144,494 | 350,691 | **41%** | 48% |
| 2026-08-06 | mixed | 15,768 | 44,465 | **36%** | **10%** |
| **whole window** | | **185,162** | **420,056** | **44%** | **43%** |

The monitor is exact when the workload is simple and blind precisely when it is complex. Grouping
the same calls by dimensions the monitor does not record shows where the missing mass lives:

| Dimension | Calls | Share of output | Share of cache read |
|---|---:|---:|---:|
| **subagents** | 293 | **29%** | **34%** |
| `entrypoint: cli` (interactive) | 463 | 59% | 65% |
| **`entrypoint: sdk-cli` (headless/SDK)** | 197 | **41%** | **35%** |

Two independent blind spots, both structural:

1. **Subagents are invisible.** The statusline payload describes only the main thread's most recent
   response. Task-tool and workflow agents never appear — 29% of output and 34% of cache reads in
   this window.
2. **Non-interactive runs are invisible.** Headless and SDK-driven sessions never render a
   statusline, so they contribute nothing — 41% of output and 35% of cache reads here. This is
   exactly the "something running in the background" a user suspects but cannot confirm.

The exact split moves with the workload: a day of heavy delegation raises the subagent share, a day
of scripted runs raises the headless share, and the worst single day above recovered only 10% of
cache reads. That variability is itself the argument for per-call collection — the blind spot is not
a stable correction factor that could be applied to an aggregate after the fact.

Two further defects follow from the same root cause — the statusline being the source of truth:

3. **Sampling loss.** Accumulation reconciles consecutive *renders*, not API calls. Any calls
   completing between two renders are collapsed into one sample.
4. **Model attribution is lossy and wrong.** A session stores a single "current model" field, so a
   session that switches models attributes everything to the last one. Tiering maps unknown model
   ids to the most expensive tier, so newer families are silently misfiled. On this machine
   `claude-fable-5` was being counted as Opus, and `claude-haiku-4-5` never appeared at all
   because it was only ever used by subagents.

A fifth constraint shapes the design: **transcripts are ephemeral.** The default
`cleanupPeriodDays` is 7, and on this machine zero transcripts older than seven days survive.
Whatever we do must harvest into durable storage before deletion.

The conclusion is that this cannot be fixed by adding fields to the existing daily aggregate. The
granularity of the source data is wrong. We need a per-call ledger.

**A caveat on this evidence.** It was all gathered from Claude Code, because that is where this
repository started and where the instrumentation already existed. It says nothing about which
harness a given user's problem lives in. The episode that prompted this design turned out to be on a
*different* harness — a subscription change and burst on Codex — which the Claude Code work would not
have explained at all. The lesson is not that the evidence is wrong but that **build order should
follow the harness the user is actually burning on**, not the one that happens to be best
instrumented. The collector contract (§5.1) exists so that ordering is a scheduling decision rather
than an architectural one.

---

## 3. Design principles

1. **One fact table.** Every model call becomes one immutable record. All views are group-bys.
   No aggregate is ever the source of truth.
2. **Idempotent by construction.** Records are keyed by provider request id. Re-scanning,
   re-syncing, relaying through a third machine, and overlapping collector runs must all be safe.
   This is what makes "arbitrary number of machines" a non-event.
3. **Sources are ranked, not assumed.** Each harness exposes several surfaces with different
   stability and completeness. We record which surface a fact came from and how much we trust each
   field, rather than pretending one source is perfect.
4. **Attribute honestly.** Where a number is estimated, calibrated, or unattributable, say so in
   the data model and in the output. An "unexplained" bucket is a feature.
5. **Degrade gracefully.** One machine with one harness and no configuration must work. Everything
   else — sync, multiple providers, high-fidelity collection — is additive.
6. **No daemon by default, no third-party dependencies.** Python standard library only, consistent
   with the rest of the repository. The optional high-fidelity collector is the single exception
   and is opt-in.
7. **Local and private by default.** Prompts and tool inputs are never stored. Labels that can leak
   client or employer identity are redactable, and export redacts by default.

---

## 4. Core model

Three record types. Everything else is derived.

### 4.1 Call records — the ledger

One record per billable model call.

```jsonc
{
  "v": 1,
  "id": "req_011Cdt6HZqbt8jSpkrQtTfGv",  // provider request id; dedup key with `provider`
  "ts": "2026-08-10T18:32:25.104Z",       // UTC, call completion
  "duration_ms": 4180,                    // null when unknown; with ts gives the active interval
  "machine": "m-7f3a2c",                  // stable local id, not the hostname
  "harness": "claude-code",               // claude-code | codex | pi | ...
  "harness_version": "2.1.226",
  "provider": "anthropic",
  "pool": "anthropic:sub:default",        // which budget this call was charged to (§4.2)
  "model": "claude-opus-5",               // verbatim provider model id, never a tier
  "session": "498a78e1-…",
  "thread": {                             // who inside the session made the call
    "kind": "subagent",                   // main | subagent | auxiliary | compaction
    "id": "a4622845bd95a5054",
    "type": "Explore",                    // agent type/name where the harness reports it
    "parent": "498a78e1-…"
  },
  "origin": "sdk-cli",                    // cli | sdk-cli | sdk-ts | sdk-py | ide | unknown
  "project": "code-battle",               // label, redactable
  "branch": "v4-003-a1-exact-contract",   // label, redactable
  "knobs": {                              // the settings that drove cost
    "effort": "max", "thinking": true, "fast": false,
    "ctx_window": 1000000, "service_tier": "standard"
  },
  "tokens": {
    "input": 2,                           // fresh, uncached
    "output": 205,                        // includes reasoning/thinking where not separable
    "reasoning": null,                    // subset of output; null when not reported separately
    "cache_read": 2800,
    "cache_write": 10742,
    "cache_write_5m": 0, "cache_write_1h": 10742
  },
  "tools": {"web_search": 0, "web_fetch": 0},
  "tool_sig": ["9f2c1a…", "9f2c1a…"],     // salted hashes of tool calls; enables loop detection
  "cost_usd": null,                       // provider-reported when available, else derived
  "confidence": {"input": "exact", "output": "exact",
                 "cache_read": "exact", "cache_write": "exact"},
  "src": "transcript",                    // otel | transcript | rollout | sdk-result | api | statusline
  "collector": "cc-transcript@1",
  "fidelity": 20                          // travels WITH the record; decides merges across machines
}
```

Design notes:

- **Dedup key is `(provider, id)`.** Where a harness exposes no request id, the collector
  synthesises `sha256(machine|session|ts|token-vector)` and sets `id_synthetic: true`; such records
  are dropped from cross-machine merges to avoid double counting relayed copies.
- **In the ledger, `output` always includes reasoning and `reasoning` is an optional breakdown** —
  but **providers disagree about this and the adapter must normalise, not assume.** Codex
  (`reasoning_output_tokens`), Pi (`usage.reasoning`) and the Anthropic API
  (`usage.output_tokens_details.thinking_tokens`) report reasoning as a *subset* of output. Gemini
  reports `thoughtsTokenCount` as a separate field that `candidatesTokenCount` **excludes** — they
  are *additive*. A collector that assumes subset semantics for Gemini would set
  `output = candidates`, satisfy every validator (since `reasoning < output`), and silently discard
  the thinking tokens, which are usually the dominant cost. The collector contract therefore requires
  each adapter to declare `reasoning_semantics: subset | additive` explicitly and convert; there is
  no safe default.
- **`confidence` is per field**, values `exact | estimated | derived | absent`. This is how the
  system stays honest when a source reports four token types accurately and one poorly. It also
  survives harness upgrades: a field can degrade without invalidating the record.
- **`thread` is the answer to "which agent burned it"**, and `origin` is the answer to "was this
  even me typing".
- **`knobs` is the answer to "did I leave something expensive on"**, and is what makes the
  effort/fast-mode diagnostics possible.
- **`fidelity` is a property of the record, not of the running process.** An earlier draft left it
  implicit in the collector class, which makes cross-machine merging undefined: a reader on one
  machine cannot rank records produced by a collector version — or a collector — it has never seen.
  Since merging across machines is the design's headline property, the rank has to travel in the
  data.
- **`duration_ms` is what makes concurrency computable.** With completion time alone you cannot
  distinguish one agent stuck for twenty-five minutes from four hundred fast calls, and "several
  agents running in parallel" is the situation this whole system exists to explain. Where the source
  reports it — the OTel `api_request` event does — it must be kept.
- **`tool_sig` holds salted hashes only**, never tool inputs. Without it the runaway detector reduces
  to a call-rate heuristic.

**A second record type is required: errors.** Rate-limit rejections, retries and refusals are
central to "what happened during that spike" — a 429 storm looks nothing like productive burn but
consumes wall-clock and provokes retries. Errors carry the same identity and dimension fields, plus
`status_code` and `attempt`, and no token counts. They are excluded from every token total and
included in diagnostics.

### 4.2 Budget pools

A pool is *the thing that gets used up*. Every call is charged to exactly one. This is the
abstraction that makes the system provider-agnostic: subscriptions, prepaid credits, and
pay-as-you-go keys are all pools, differing only in how their remaining balance is observed.

```jsonc
{
  "pools": {
    "anthropic:sub:default": {
      "provider": "anthropic", "kind": "subscription",
      "meter": "rolling_pct", "windows": ["five_hour", "seven_day"],
      "plan": "max-20x"                       // user-declared, used only for context in reports
    },
    "openrouter:api:main":  {"provider": "openrouter", "kind": "api", "meter": "credit_balance"},
    "openai:api:work":      {"provider": "openai",     "kind": "api", "meter": "spend"},
    "local:workstation":    {"provider": "local",      "kind": "self_hosted", "meter": "none"}
  }
}
```

| Meter | Means | Observed from | Unit |
|---|---|---|---|
| `rolling_pct` | share of a rolling-window allowance consumed | harness surfaces that expose rate limits | % |
| `credit_balance` | prepaid balance remaining | provider API | currency |
| `spend` | cumulative spend | provider usage API, or derived from a price table | currency |
| `none` | not metered (self-hosted) | — | — |

Pool assignment is a function of `(harness, provider, credential fingerprint)`. The fingerprint is
an HMAC of the credential using a machine-local salt — enough to distinguish "my work key" from
"my personal key" and to keep them stable across machines when the same key is used, without ever
storing the key.

### 4.3 Meter readings

```jsonc
{"v": 1, "ts": "2026-08-10T18:32:25Z", "machine": "m-7f3a2c",
 "pool": "anthropic:sub:default", "meter": "rolling_pct",
 "readings": {
   "five_hour": {"used_pct": 29, "resets_at": 1780093200},
   "seven_day": {"used_pct": 52, "resets_at": 1780353000}
 }}
```

The critical property: **a meter reading is an account-global observation, but the ledger is
per-machine.** Any machine can observe the whole account's remaining allowance; no machine can
observe another machine's calls. That asymmetry is what makes cross-machine drain *detectable*
even when the other machine is not reporting — see §8.

**This is an assumption and it is load-bearing.** Rolling-window limits are enforced server-side
across an account, and the client learns its consumption from the API response, so the reading
should reflect every device. It has not been verified here, and Claude Code's own documentation
notes that the in-app usage view is not aggregated across devices — which most plausibly refers to
its locally computed session token and cost figures rather than the server-supplied window
percentages, but the distinction has not been confirmed. **Verify before building §8.2 on it**: read
the same window's percentage from two machines within a short interval and check they agree. If they
do not, the residual mechanism collapses to per-machine reconciliation and cross-machine drain
becomes detectable only through coverage reporting (§8.3), which is why that path is specified as
the primary answer rather than the fallback.

`resets_at` also gives exact window boundaries, so reports can align to the windows the user is
actually limited against rather than to calendar days. It is a **Unix epoch integer**, not an
ISO-8601 string.

### 4.4 What the meter actually behaves like

Three measured properties, from 3,845 recorded readings on one machine over 3.6 days. Each one
constrains what can honestly be built on top, and together they are the reason §8 ends where it
does.

- **`used_percentage` is quantized to whole integers.** Consecutive readings step 67, 68, 69. Any
  quantity derived from a single interval's change is therefore carrying ±1 point of rounding noise
  — often larger than the signal.
- **It is not monotone inside its own window.** The value *decreased* without `resets_at` changing
  **214 times on the 5-hour window and 42 times on the 7-day window**. This is not a bug: the windows
  are rolling, so old consumption ages out continuously. A window's change over an interval is
  therefore `burn added − burn expired`, and any model that treats it as `burn added` alone is
  mis-specified.
- **Sampling is dense when observed and absent otherwise.** Median gap between readings 2 seconds,
  90th percentile 12 seconds, **maximum gap 20.3 hours**. Readings exist while an interactive session
  renders and nowhere else — which means the meter is blind during exactly the unattended stretches
  the system exists to investigate.

A fourth property appears once a second harness is examined, and it is worse:

- **Concurrent sessions publish snapshots at different staleness, and the reset timestamp is not a
  window identifier.** In Codex rollouts over three days there were **162 distinct `resets_at` values
  for the single weekly window**, nine sessions writing readings simultaneously, and a naive
  time-ordered merge of their snapshots produced apparent jumps of **+83 percentage points in 1.1
  seconds**. None of that is consumption. Two conclusions follow. First, `resets_at` on a *rolling*
  window is a derived "when capacity returns" estimate that slides continuously as old usage ages
  out — it cannot be used as a key to group readings into window instances, which an earlier draft of
  this document assumed. Second, a meter series has to be reconstructed per publishing session and
  reconciled as "latest known value", never concatenated across concurrent publishers.

Plan changes compound this: cancelling and replacing a subscription resets the denominator, so the
series is genuinely discontinuous and no amount of care makes it a single continuous curve.

**The design consequence is significant.** The meter is usable for answering "how much of my
allowance is left right now", which is what a status line needs. It is not a reliable spine for
attribution over time. Anything that needs a burn *curve* should be built on the ledger's own
token rate, which is clean, monotone by construction, and available at per-call resolution — see §9.

---

## 5. Collection

### 5.1 Collector contract

Adding a harness means adding one module and its tests. No core change.

```python
class Collector:
    name: str                                    # "claude-code/otel"
    fidelity: int                                # higher wins when sources overlap
    def available(self) -> Availability          # installed? configured? degraded?
    def collect(self, cursor) -> Iterator[Call]  # incremental, resumable
    def meters(self) -> Iterator[MeterReading]   # optional
```

Collectors are incremental: each keeps a cursor of `(path, size, mtime, byte offset)` or an
equivalent, so a run costs time proportional to new data, not to history. This matters — the
current exporter rewrites an 11 MB file every fifteen minutes and syncs it whole.

Merging two records with the same `(provider, id)` follows three rules, in order:

1. **Higher `fidelity` wins, but by coherent field group, not by individual field.** The four token
   counts move together; the cache-write total and its 5m/1h split move together. Mixing a total
   from one source with a split from another produces a record that violates its own invariant —
   the splits no longer sum to the total — and does so *after* validation has already run. Group
   merging prevents that; the merged record is revalidated regardless.
2. **Equal fidelity resolves by maximum per token field.** This is the same rule the transcript
   collector needs internally (§5.2) and for the same reason: a record may have been written from an
   incomplete read. Maximum is order-independent and idempotent, so it is safe under retries,
   re-scans and duplicate delivery.
3. **A lower-fidelity record may fill field groups the winner left `absent`**, and that is recorded
   in `confidence` rather than presented as if the winner had reported it.

### 5.2 Claude Code

Four surfaces, in descending fidelity. This is the layering that keeps the system both accurate and
zero-config.

| # | Surface | Stability | Per call | Subagents | Headless | Quota | Setup |
|---|---|---|---|---|---|---|---|
| 1 | **OpenTelemetry** *(log events, not metrics)* | documented, supported | yes | yes | yes | no | opt-in receiver |
| 2 | **Transcripts** | documented location, **explicitly unstable schema** | yes | yes | yes | no | none |
| 3 | **Statusline payload** | documented | partial | no | no | **yes** | already installed |
| 4 | **SDK / print-mode result** | documented | per run | yes (`modelUsage`) | yes | no | wrapper |

**(1) OpenTelemetry — the high-fidelity path.** The usable signal is the **`api_request` log event**,
which carries `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, `cost_usd`, `duration_ms`, `request_id`, `effort`, `speed`, `query_source`
and `agent.name` — the ledger record, officially supported, covering subagents and headless runs.
The companion `api_error` event supplies the error records.

**Not the metrics.** `claude_code.token.usage` is a *counter* exported on an interval (60 s by
default) and carries **no request id**. Ingesting metric datapoints as ledger rows would double-count
a cumulative series. The receiver must consume logs (`OTEL_LOGS_EXPORTER`), and metrics are at most
a cross-check.

Consuming this means accepting OTLP, since there is no file exporter. The proposal is a **~200-line
loopback-only OTLP receiver in the standard library** that appends straight to the ledger. Four
operational constraints, none optional:

- **Do not hijack the global endpoint.** `OTEL_EXPORTER_OTLP_ENDPOINT` is a single variable; pointing
  it at this receiver silently disables any collector the user already runs — and anyone willing to
  enable telemetry is disproportionately likely to have one. Use the per-signal
  `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`.
- **Do not take the default port.** 4318 is the OTLP/HTTP default and is often already bound. Choose
  a port, record it in config, and fail loudly on a bind conflict rather than silently not receiving.
- **Assume the receiver will be down sometimes.** Exporters buffer in memory, bounded, then drop —
  silent loss on the highest-fidelity source. OTel is therefore strictly *additive*: the transcript
  collector keeps running and backfills any gap.
- **Verify JSON encoding is supported before committing to this.** The design assumes
  `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`; if the logs exporter only speaks protobuf, a stdlib-only
  receiver is a much larger undertaking and this path needs rethinking.

**(2) Transcripts — the zero-config default.** Scan `~/.claude/projects/**/*.jsonl`, *including*
`<session>/subagents/agent-*.jsonl`. Verified field mapping on v2.1.226:

| Ledger field | Source |
|---|---|
| `id` | `requestId`, falling back to `message.id` |
| `model` | `message.model` (verbatim; skip `<synthetic>` error placeholders) |
| `thread.kind` | `subagents/` path segment and `isSidechain` |
| `thread.id` / `thread.type` | `agentId` / `attributionAgent` |
| `thread.parent` | `parentUuid` chain to the spawning entry |
| `origin` | `entrypoint` (`cli`, `sdk-cli`, …) |
| `knobs.effort` | top-level `effort` |
| `knobs.service_tier`, `knobs.fast` | `message.usage.service_tier`, `message.usage.speed` |
| `tokens.cache_write_{5m,1h}` | `message.usage.cache_creation.ephemeral_{5m,1h}_input_tokens` |
| `tools` | `message.usage.server_tool_use` |
| `project`, `branch` | `cwd`, `gitBranch` |

Three hard requirements for this collector:

- **Deduplicate by `requestId`, taking the maximum per token field — not the last line.** Claude Code
  writes one line per content block of a turn, all sharing one request id. The often-repeated claim
  that those lines carry *identical* usage is **false for output**: measured here, 199 of 583
  multi-line request ids carry differing usage, because early lines hold a placeholder
  `output_tokens` (values of 2–4 are typical) and only the terminating line carries the real count.
  One real example progresses `output: 4 → 4 → 254` under a single request id.

  Last-write-wins is correct only if the collector always sees the complete turn. A scheduled
  collector will routinely read a file mid-turn, record `output: 4`, and on the next run see the same
  request id again — at which point the deduplication rule decides whether the placeholder or the
  real value survives. Taking the maximum per field is order-independent, split-independent, and
  idempotent. It is also safe: on this corpus the last line equals the per-field maximum for
  **694 of 694** request ids, so max never disagrees with a complete read, and it recovers the
  correct value from an incomplete one.

  Deduplication itself remains essential — 807 raw lines collapse to 334 real calls, and summing raw
  lines overcounts cache reads by roughly 2.2×.
- **Treat the schema as unstable.** Anthropic documents this format as internal and subject to
  change on any release. The collector therefore validates each file against an expected shape,
  records `harness_version`, and on drift emits a data-quality event and degrades affected fields
  to `absent` rather than silently producing wrong numbers.
- **Run before retention deletes.** With `cleanupPeriodDays: 7`, a weekly-or-slower schedule loses
  data permanently. `doctor` warns when the last successful collection is older than half the
  configured retention.

Field accuracy has improved substantially since this repository's 2026-02 findings: on v2.1.226,
`input_tokens ≤ 1` on only 6% of deduplicated calls (previously 75–93%), and transcript output
matched statusline output within 1% on a full quiet day and 0.99–1.12× per session on a busy one.
The historical "transcript output excludes thinking, expect ~3×" correction **no longer applies**
and must not be hard-coded. `FINDINGS.md` needs updating; the calibration must be measured at
runtime, not assumed.

**(3) Statusline — demoted to renderer and meter.** It stops being an accounting source. All the
call-boundary detection, midnight baseline carry-forward and delta bookkeeping in `update_daily`
exists only because the statusline was the only source; with a ledger it can be deleted. What the
statusline uniquely provides and must keep doing is recording `rate_limits.{five_hour,seven_day}`
readings — the **only** documented local read of subscription quota. It renders from a small
derived index so it stays fast.

**(4) Print-mode wrapper.** For scripted runs, `claude -p --output-format json` returns a result
whose `modelUsage` includes subagents broken down per model. A wrapper collector captures this for
automation that runs where no collector is scheduled. Note `--max-budget-usd` also exists as a hard
per-run spend cap, which the guardrails work (§9) should surface rather than reimplement.

**Known unobservable.** The separate per-model (Opus) weekly limit is visible only inside the
interactive `/usage` view — not in the statusline payload, OTel, or hooks. The numeric thinking
budget is likewise not exposed, only a boolean. Reports must not imply otherwise.

### 5.3 Other harnesses

The same contract, one module each. Existing repository knowledge (`research/multi-cli-support.md`)
already maps the normalisation problem; the ledger is where it lands.

- **Codex CLI** — session rollouts under `~/.codex/sessions/` carry per-turn and cumulative usage at
  `payload.info.{last,total}_token_usage.{input_tokens, cached_input_tokens,
  cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}`. Two corrections
  to this repository's earlier research, which recorded that Codex exposes no cache writes and no
  subagent structure — **both are now present**: `cache_write_input_tokens` exists, and
  `session_meta.payload.source.subagent.{parent_thread_id, depth, agent_nickname, agent_role}` plus
  `thread_source ∈ {user, subagent, automation}` map directly onto `thread` and `origin`. Rate
  limits arrive as `payload.rate_limits.{primary, secondary}`, each with `used_percent`,
  `window_minutes` and `resets_at`, alongside `credits.{has_credits, unlimited, balance}` — so a
  Codex pool can carry a rolling-percentage meter and a credit meter simultaneously. **Identify the
  window by `window_minutes`, never by field position**: a live rollout inspected here had the
  weekly window (`window_minutes: 10080`) in `primary` with `secondary: null`, so reading `primary`
  as "the 5-hour window" would mislabel weekly quota as five-hour. SQLite state exists alongside the
  rollouts and is actively written; its role is **unverified**, so the collector must confirm which
  store is authoritative before relying on the JSONL.
- **Pi** — per-turn `message.usage.{input, output, cacheRead, cacheWrite, reasoning, totalTokens}`
  plus `message.provider`, `message.api` and `message.model`, and a per-call `usage.cost` breakdown
  Pi computes locally. Pi is the clearest demonstration of why `provider` and `pool` are per call
  rather than per harness: one harness routes to a hosted subscription, a credit-based aggregator
  and a self-hosted model, sometimes within one session.
- **Gemini CLI and others** — accommodated by the same contract; the OTel-style file/metric surface
  described in the existing research maps cleanly onto the collector interface.

Codex also supports OTLP export via an `[otel]` block in its own configuration, disabled by default.
That matters for E10: the receiver proposed for Claude Code is not Claude Code-specific — one
loopback OTLP endpoint can serve every harness that speaks OTLP, which makes the high-fidelity path
a shared component rather than a per-harness cost.

### 5.4 Providers

The pool abstraction absorbs the differences. What varies is only how a meter is read and whether
the provider reports authoritative cost.

| Provider | Access | Meter | Read from | Provider-reported cost? |
|---|---|---|---|---|
| Anthropic | subscription | `rolling_pct` (5h, 7d) | Claude Code statusline payload | no — derive from price table |
| Anthropic | API key | `spend` | org usage/cost API, **org accounts only** | no for individuals — derive |
| OpenAI | subscription (Codex) | `rolling_pct` + `credit_balance` | Codex rollout `rate_limits` | no |
| OpenAI | API key | `spend` | org usage/cost API, **org accounts only** | no for individuals — derive |
| OpenRouter | API key | `credit_balance` | `GET /api/v1/key` | **yes** — inline `usage.cost`, and `GET /api/v1/generation` |
| Self-hosted | local | `none` | — | n/a — energy is the only currency |

Two facts shape this more than they might appear to:

- **Neither Anthropic nor OpenAI offers a historical usage or cost API to individual accounts** —
  those endpoints are organisation-scoped and require an admin key. For an individual on a
  subscription there is no server-side record to reconcile against, which is precisely why the local
  ledger has to be the durable artefact rather than a cache of something authoritative.
- **Subscription quota is observable, but only through a harness.** There is no REST endpoint for
  it on either side; the numbers exist because Claude Code writes them into its statusline payload
  and Codex writes them into its rollout. That makes meter collection dependent on a harness
  actually running, which §8.3 treats as a first-class limitation rather than an edge case.

OpenRouter is the exception that proves the model useful: it reports exact per-generation cost and
native token counts including cached and reasoning tokens, so its records land with
`confidence: exact` and `cost_usd` populated, and its credit balance reconciles arithmetically
rather than by regression.

Where a provider reports exact per-call cost, `cost_usd` is populated and marked `exact`; otherwise
it is derived from a versioned price table and marked `derived`. The price table lives in one
place, is dated, and is never silently updated — a changed price must not retroactively rewrite
history.

### 5.5 A worked mixed setup

Harness, provider and pool are three independent axes, and all three vary *within* a single machine.
A concrete configuration the design must handle without special-casing:

| Machine | Harness | Provider | Pool | Meter |
|---|---|---|---|---|
| laptop | Claude Code | Anthropic (subscription) | `anthropic:sub:default` | rolling % |
| laptop | Codex CLI | OpenAI (subscription) | `openai:sub:default` | rolling % |
| laptop | Pi | OpenRouter (API key) | `openrouter:api:main` | credit balance |
| home server | Claude Code (headless) | Anthropic (**same** subscription) | `anthropic:sub:default` | rolling % |
| home server | Pi | self-hosted model | `local:home-server` | none |
| work laptop | Claude Code | Anthropic (**API key**, different budget) | `anthropic:api:9f2c` | spend |

Three consequences fall out of this, and each is a design requirement rather than an afterthought:

- **One harness spans providers.** Pi appears twice with different providers, so `provider` and
  `pool` must be per call, never inferred from the harness.
- **One pool spans machines and harnesses.** The subscription pool is drained by two machines, so
  attribution must be merged across machines before it means anything. If the account-global reading
  assumed in §4.3 holds, a burn on the home server also shows up in the laptop's quota reading even
  when the home server is not reporting — which is what makes an unseen drain detectable rather than
  merely suspected.
- **One provider spans pools.** Anthropic appears as both a subscription and a separate API key with
  its own budget. Splitting them requires the credential fingerprint, which is why pool assignment
  keys on it rather than on the provider name.

Reports are always scoped to a pool, because "how much have I used" only means something relative
to a specific allowance. Tokens and energy can be summed across pools; percentages and money cannot.

---

## 6. Three currencies

Attribution is reported in whichever of these the user asks for, and the system never blends them
into a single invented number:

1. **Tokens** — always available, directly comparable within a provider.
2. **Energy (Wh)** — the repository's existing order-of-magnitude proxy, per token type and model
   tier. Cross-provider comparison remains out of scope and is labelled as such.
3. **Money** — provider-reported where available, otherwise derived from the price table. For
   subscription pools this is an *equivalent* cost ("what this would have cost at API rates"),
   which is genuinely useful for deciding whether a subscription tier is right, and must be
   labelled as equivalent rather than charged.

Quota percentage is deliberately *not* a fourth currency; it is a property of a pool's meter and is
handled by reconciliation.

---

## 7. Multi-machine topology

The sync layer moves files. Nothing more.

```
<sync_root>/
  machines/<machine_id>/
    calls/YYYY-MM-DD/000042.jsonl   immutable chunk, never reopened after write
    meters/YYYY-MM-DD/000007.jsonl
    manifest.json                   {chunk path: {bytes, sha256}} — the only index readers trust
    heartbeat.json                  {machine, ts, collectors, harnesses, versions, last_success}
```

**Chunks, not appended day files.** A day file that is still being appended to cannot be
distinguished from a complete one by a reader on another machine, and no sentinel exists to test for
a partial write. Sequence-numbered chunks that are written once and never reopened, listed in a
manifest with a size and hash, make "is this safe to read" a decidable question. Readers ignore any
file not in the manifest or not matching the chunk pattern — which is also what makes cloud-storage
**conflicted copies** (`… (host's conflicted copy).jsonl`) inert rather than a source of
double-counted records. That matters specifically for records with synthetic ids, which cannot be
deduplicated and which a same-machine conflicted copy would otherwise duplicate.

The day partition is the **UTC date of the record's `ts`**, fixed independently of local clocks, so
that "closed day" means the same thing on every machine and does not shift under clock skew or a
resume from suspend.

Because merging deduplicates on `(provider, id)`, duplicate delivery is otherwise harmless —
relaying through a third machine, retried transfers and overlapping runs are all safe by
construction.

**Machine identity must survive being cloned.** An id persisted only to a file is duplicated by a
backup restore, a cloned VM or a container image, producing two live writers on one path. Bind it to
a boot or hardware identifier so clones diverge, and treat two heartbeats claiming one id as an
error surfaced to the user rather than silently interleaved data.

Transports are pluggable and none is privileged: SSH/rsync, a shared folder (Dropbox, iCloud Drive,
Syncthing, a mounted NAS), a git repository, or object storage. Shipping *both* SSH and
shared-folder transports matters for the public case: requiring SSH between personal machines is a
significant barrier, and a synced folder is the zero-setup answer for most people. Cloud folders
bring their own failure mode worth handling explicitly — iCloud Drive evicts files to `.icloud`
placeholders, so a glob can return stubs or nothing at all with no error, which the manifest turns
into a detectable missing-chunk condition instead of a silent undercount.

Three operational fixes are in scope here, from a recorded production incident where scheduled sync
runs accumulated to ~110 overlapping processes:

- single-instance locking with stale-lock recovery,
- bounded connect and transfer timeouts,
- per-host isolation so one unreachable machine cannot stall the others, with explicit
  skipped/timed-out/failed status per host.

**Heartbeats are load-bearing, not decoration.** A machine that stops reporting is the single most
likely explanation for unattributed burn, so `heartbeat.json` is what turns "I can't explain this"
into "machine *X* hasn't reported since 14:05".

---

## 8. Attribution and reconciliation

### 8.1 The cube

All reports are group-bys over the ledger:

```
time window × pool × machine × harness × provider × model
            × thread.kind × thread.type × session × project × origin × knobs
            × token type
```

Time windows align to the meter's own windows when one exists (`resets_at`), falling back to local
days.

**Aggregation must carry absence, not swallow it.** A measure that a source cannot report is
`absent`, and `absent` is not zero. Summing a mixed corpus where one harness does not report cache
writes produces a total that is wrong by exactly the amount it cannot see, and prints as if it were
complete. Every aggregate therefore returns `(value, n_records, n_absent)` per measure, and every
renderer is required to mark a figure whose `n_absent > 0`. Grouping *dimensions* get an explicit
`unknown` bucket for the same reason.

**On scale: the data is small and should be treated as small.** A full reparse of every retained
transcript on this machine — 22 files, 658 deduplicated calls — takes **0.04 seconds**, and the
seven-day retention caps the corpus so it does not grow. Incremental cursors and derived indexes are
optimisations of a 40-millisecond operation and should not be built until a corpus is *measured*
slow. The durable ledger is justified by permanence and multi-machine merging, not by performance.

### 8.2 Explained versus observed

For a pool with an observable meter, over a window:

```
observed  = meter movement across the window (from readings, any machine)
explained = the ledger's contribution over the same window
residual  = observed − explained
```

For `credit_balance` and `spend` pools both sides are in currency and this is arithmetic. This is
where reconciliation genuinely works, and it should be built first.

For `rolling_pct` pools it is much harder than it looks, and an earlier draft of this document got
it wrong. The tempting approach — regress observed `Δused_pct` on the four-way token vector to learn
percent-per-token coefficients — fails on all three properties measured in §4.4:

- **the regressand is mis-specified**, because Δ is `burn added − burn expired` and the expiry term
  is unobservable, so intervals where old burn ages out present positive tokens against a negative
  Δ and teach the fit negative coefficients;
- **the regressand is almost pure rounding noise**, because whole-integer quantization means Δ per
  interval is 0 or ±1;
- **the design matrix is collinear**, because in agentic workloads input, output, cache read and
  cache write move near-proportionally, so a four-way fit returns large opposite-signed coefficients
  that fit noise.

The failure mode is not "no answer" but *a confident wrong one*: a fitted negative weight on cache
reads yields a negative explained burn, and the tool reports that most of the week was consumed by
an invisible machine when nothing else ran. That is worse than saying nothing, because it is exactly
the conclusion the user was already worried about and would believe.

What is defensible instead:

1. **Fix the relative weights, fit only a scale.** Use published price ratios as a fixed cost weight
   — `advisor.py` already does this — and fit a *single scalar* mapping weighted tokens to
   percentage points. One parameter against a noisy integer series is estimable; four are not.
2. **Restrict the fit to admissible intervals**: no observed decrease, and a change of at least
   three points so quantization is not dominant.
3. **Report an interval, never a point**, and hard-fail on a negative or implausible scale rather
   than publishing it.
4. **Never claim per-model attribution of quota.** The provider enforces a separate limit for its
   largest model that is not exposed anywhere programmatically, so per-model quota shares cannot be
   derived and must not be implied.

Even done well this yields a coarse cross-check, not a precise residual. It is deliberately the
*last* thing built, and the system is designed to be useful without it.

### 8.3 Coverage — the answer that always works

Calibration can fail; coverage cannot. For any window the system can always state which machines
reported, when each last succeeded, which collectors ran, and what share of burn each explains.
A silent machine, a collector that has not run since Tuesday, or a harness with no collector
installed are all reported as *named, specific* gaps.

This is the v1 *and* the durable answer to "is something draining my tokens", not a placeholder.
"I can explain 78% of this window's tokens; machine *X* has not reported for 6 hours; you have no
collector installed for the harness you ran yesterday" is more useful and more honest than a
confident number derived from a weighting that §8.2 shows cannot be estimated reliably.

### 8.4 The ceiling

Two things this design cannot do, stated here rather than left to be discovered:

- **It cannot express a session's cost as a share of your allowance.** Reports are in tokens, energy
  and equivalent money. Converting to "percent of my week" requires the token-to-quota mapping that
  §8.2 shows is barely estimable, so `why`, the knob-impact rule and the counterfactual all answer
  "how many tokens would this have saved", not "how many percent". That is a real gap, because
  percent-of-allowance is the unit the user actually thinks in.
- **It cannot name an unattributed consumer.** Coverage can say a machine is silent, and can say a
  share is unexplained. It cannot distinguish "the web app", "a phone", or "a machine that has never
  run this tool" from each other. Naming them would require the reconciliation §8.2 rules out.

---

## 9. Diagnostics

Rules over the ledger, each emitting a structured finding with its evidence so terminal, HTML and
JSON renderings share one implementation.

| Rule | Answers |
|---|---|
| **Window attribution** | Ranked contributors for the active 5h/7d window by machine, session, agent, model, project. |
| **Coverage and liveness** | Which machines and collectors are reporting; what share is unexplained. |
| **Automation share** | Burn from `origin != cli` — "41% of this window's output came from runs you did not type". |
| **Concurrency** | How many agents were actually in flight over time, from `ts` and `duration_ms`. Separates "one agent stuck for 25 minutes" from "400 fast calls" — the most direct answer to a burst across parallel agents. |
| **Runaway detection** | Sustained call rate above the session's own baseline; repeated `tool_sig` hashes; cache-read per call flat and high while output per call collapses; retry storms from error records. Requires `tool_sig` and the error record type — without them this rule degrades to the rate signal alone, and should say so rather than appear complete. |
| **Knob impact** | Burn per call grouped by effort, fast mode and thinking, holding model and project fixed — "xhigh averaged N× the output per call of high". |
| **Model counterfactual** | Re-price the window under a substitution policy — "routing subagent work to the small model would have cut explained burn by N%". |
| **Fan-out cost** | Subagent share, agents per session, cost per agent type. |
| **Context growth** | Cache read per call against session age; identifies sessions that should have been restarted. |
| **Spike attribution** | Find the steepest segment of the **ledger's own token burn rate** and report everything running during it. Deliberately *not* the meter curve: the meter is quantized, non-monotone, discontinuous across plan changes, and corrupted by concurrent stale publishers (§4.4), whereas per-call token rate is clean at full resolution. This also means spike attribution needs no calibration at all — for a concentrated burst, "here is everything that ran between 14:02 and 14:31, ranked" is a complete answer. |

The runaway detector stores only salted hashes of tool inputs, never the inputs themselves.

The counterfactual and knob-impact rules are what turn a diagnosis into a decision — a smaller
model, a lower effort level, fewer agents, or something switched off. Without them the system
reports where the tokens went but not what to do about it, which is only half the question in §1.

---

## 10. Presentation

| Surface | Purpose |
|---|---|
| `why` | The flagship. "Where did my tokens go in this window?" — attribution, residual, findings, ranked. |
| `report` | Periodic summary; `--html` writes a single self-contained file with inline SVG, no dependencies and no server. |
| `watch` | Live burn rate and projected exhaustion. |
| `doctor` | Coverage, collector health, retention risk, sync status, calibration quality. |
| `export` | Redacted, shareable extract. |
| statusline segment | `7d:52% ▲9%/h → full in 5.3h`, rendered from the derived index. |

The statusline keeps its existing one-line contract; the attribution segment is additive and
optional.

---

## 11. Privacy

The tool is public and will run on machines with client work on them.

- Prompt text, assistant output and tool inputs are **never** stored. The repetition detector uses
  salted hashes only.
- `project`, `branch`, `session_name` and agent names can identify an employer or client.
  `privacy.labels` selects `plain | hashed | omit`; `export` and shared HTML redact by default.
- Credentials are never read for their value. Pool identity uses an HMAC fingerprint with a
  machine-local salt.
- Account identifiers are hashed before storage.
- All files `0600`, consistent with the existing monitor.
- The OTLP receiver binds loopback only and accepts no remote connections.

---

## 12. Migration

No user data is deleted or rewritten in place.

1. **Shadow.** Ledger and collectors ship alongside today's files. `doctor` reports drift between
   the two. Nothing user-visible changes.
2. **Cut over reads.** `statusline`, `stepcount` and `advisor` read the derived index. Legacy files
   keep being written.
3. **Import history.** Existing `statusline_history.jsonl`, session history and per-machine journals
   are imported as day-level records with `confidence: aggregate` and no thread or knob dimensions,
   so historical totals survive while being clearly distinguishable from ledger-grade data. **The
   import needs a machine-id mapping**: legacy journals default their machine id to the hostname, so
   importing them naively both injects hostnames into a store that is meant not to contain them and
   makes one physical machine appear as two in every machine-grouped report.
4. **Retire.** `update_daily`'s accumulation is removed. `pi_*` filenames — which mean "headless
   scanner", not "Raspberry Pi" — are renamed with compatibility shims retained for one release.
   Renaming must preserve the property that the local export filenames deliberately do *not* match
   the glob used to discover remote files; that naming is load-bearing and a careless rename
   reintroduces a double-count this repository has already been bitten by once.

Two existing surfaces need an explicit decision rather than being left to drift:

- **`advisor.py` already implements a quota breakdown** — by project, by model, top sessions by
  estimated quota impact, and a quota-driver analysis — over the same question this design addresses.
  Either `why` supersedes it and it is deleted, or the two diverge into competing answers. It should
  supersede it, and its nine advisory rules should be migrated into §9's rule set rather than
  reimplemented.
- **The storage root.** Everything today lives in `~/.claude/`, which is another tool's directory. A
  harness-agnostic tool should not put a Codex-only user's data there. Choosing the new root is part
  of this work, not an afterthought.

---

## 13. Open questions

1. **Is `(provider, request_id)` really the billing grain?** The transcript's `message.usage`
   contains an `iterations[]` array mirroring the usage fields. If that denotes multiple billable
   inference passes within a single request id, the dedup key is wrong and the whole ledger
   under-counts. **Resolve before freezing the schema** — everything else assumes this.
2. **Quota weighting.** Whether even the single-scalar calibration in §8.2 is stable enough to
   publish, or whether coverage reporting is the permanent ceiling. Needs weeks of readings, and the
   sampling gaps in §4.4 may make it unanswerable.
3. **Are quota readings account-global?** §4.3 depends on it and it is unverified. Cheap to settle:
   read the same window from two machines minutes apart.
4. **Transcript schema drift.** The format is explicitly unstable. How aggressively should the
   collector fail closed? Proposal: degrade fields, never guess, always surface.
5. **The OTLP receiver's status.** Opt-in service, or eventually the default with the transcript
   scanner as fallback? Depends on how painful the receiver proves in practice, and on whether its
   logs exporter speaks JSON at all.
6. **Naming.** The repository is Claude Code-specific by name but already covers three harnesses.
   A single neutral CLI entry point is proposed; whether the repository itself is renamed is a
   separate decision.
7. **Per-model quota limits.** The separate weekly limit for the largest model is unobservable
   programmatically. If that changes, reconciliation should model pools per model family.

---

## Appendix: verified field inventory

Confirmed on this machine, 2026-08-10, Claude Code v2.1.226. Fields marked *documented* are
guaranteed by published documentation; fields marked *observed* were read from live data and belong
to a format documented as internal and unstable.

**Statusline payload** *(documented)* — `model.{id,display_name}`, `session_id`, `session_name`,
`transcript_path`, `cwd`, `version`, `workspace.{current_dir,project_dir,added_dirs,git_worktree,repo}`,
`cost.{total_cost_usd,total_duration_ms,total_api_duration_ms,total_lines_added,total_lines_removed}`,
`context_window.{total_input_tokens,total_output_tokens,context_window_size,used_percentage,remaining_percentage,current_usage.*}`,
`exceeds_200k_tokens`, `fast_mode`, `effort.level`, `thinking.enabled`, `output_style.name`,
`rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`, `agent.name`, `pr.*`, `worktree.*`.

**Transcript assistant entry** *(observed)* — top level: `requestId`, `sessionId`, `uuid`,
`parentUuid`, `isSidechain`, `timestamp`, `type`, `effort`, `userType`, `entrypoint`, `cwd`,
`version`, `gitBranch`, `slug`; subagent files add `agentId` and `attributionAgent`.
`message.usage`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`,
`server_tool_use.{web_search_requests,web_fetch_requests}`, `service_tier`, `speed`,
`inference_geo`, `iterations[]`.

**OTel** *(documented)* — metric `claude_code.token.usage` with attributes `type`
(`input|output|cacheRead|cacheCreation`), `model`, `query_source` (`main|subagent|auxiliary`),
`speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`,
`session.id`, `app.entrypoint`, `user.account_uuid`, `organization.id`; metric
`claude_code.cost.usage` in USD; event `api_request` with `model`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `duration_ms`, `request_id`, `effort`,
`speed`, `query_source`; event `api_error` with `status_code` and `attempt`.

**Hooks** *(documented)* — no usage or token fields on any hook payload. `SubagentStop` provides
`agent_id`, `agent_type` and `agent_transcript_path`; `prompt_id` joins hook events to OTel
`prompt.id`. No hook fires per API request.
