# Usage Attribution — Epics and Tickets

Implementation breakdown of [`token-attribution-architecture.md`](token-attribution-architecture.md).

Written to be executed by an implementing agent working one ticket at a time. Every ticket states
its scope, the files it may touch, acceptance criteria, and what is explicitly out of scope. Unless
a ticket says otherwise: Python 3.10+, standard library only, files created `0600`, one branch and
one PR per ticket, and a failing test written before the implementation.

## Milestones

| Milestone | Contents | Delivers |
|---|---|---|
| **M0 — Answer it, statelessly, on both harnesses** | E0 (Claude Code **and** Codex) | One command, no stored state, that explains a window. A *shipping* answer, not a throwaway. |
| **M1 — Make it durable** | E1, E2, **T9.1**, E6.1–E6.3, E7.1–E7.2, T7.6 | The same answer, surviving the 7-day transcript retention, for both harnesses, with coverage reporting |
| **M2 — Quota context** | E3, E7.3–E7.4, E6.5–E6.6 | Live burn rate, runaway and knob diagnostics |
| **M3 — Many machines** | E5, E7 remainder, E8.1 | Account-wide view, named gaps |
| **M4 — Remaining providers and harnesses** | E4, E9.2–E9.3 | Pi, API and credit pools, money as a currency |
| **M5 — Fidelity and release** | E10, E8.2–E8.3, E11 | OTel path, coarse quota cross-check, public-ready repository |

Sequencing: **do E0 first and put it in front of a real user**, then **E1 blocks everything else**.
E2 blocks M1. Nothing in E4/E9/E10 may change the ledger schema without a versioned migration ticket.

**Codex is in M0, not M4.** The first draft sequenced every milestone around Claude Code because that
is where the existing instrumentation and all the measured evidence came from. That was wrong for the
episode that prompted this work, which was a subscription change and burst on **Codex** — four
milestones of Claude Code tooling would not have explained a single token of it. Codex's per-call
data is in some respects better than Claude Code's (per-turn deltas rather than snapshots, an
explicit reasoning split, cache writes, `model_provider`, and a real subagent spawn structure), so
there is no technical reason to defer it either. The general rule: **build the harness the user is
actually burning on first.**

**Why M0 is a real milestone and not a spike.** A full reparse of every retained transcript on the
development machine — 22 files, 658 deduplicated calls — takes **0.04 seconds**, and the seven-day
retention caps the corpus so it does not grow. A stateless command that reparses on every invocation
is therefore not a compromise; it is the right implementation at this scale. E1's durable ledger is
justified by two things a stateless command genuinely cannot do — surviving retention, and merging
across machines — and by nothing else. Build it for those reasons, not for speed.

## Corrections applied after review

An adversarial review of the first draft found several claims that measurement contradicted. Anyone
implementing from an earlier copy should note:

| Was | Is |
|---|---|
| Content-block lines share identical usage; dedup by last-write-wins | 199 of 583 multi-line request ids carry **differing** usage; dedup by **max per token field** (T2.2) |
| Learn quota weights by regressing Δpercent on the token vector | The meter is non-monotone, integer-quantized and collinear — fit **one scalar** on fixed price-ratio weights, or nothing (T8.2) |
| `resets_at` is an ISO-8601 string | It is a **Unix epoch integer** (T6.2) |
| OTel gives per-call data | Only the **log events** do; the token metric is a counter with no request id (E10) |
| `fidelity` belongs to the collector | It must **travel on the record**, or cross-machine merge is undefined (T1.1) |
| Reasoning is a subset of output everywhere | **Gemini is additive**; adapters must declare which (T1.1, T9.3) |
| `resets_at` identifies a window instance | It slides continuously — **162 distinct values in 3 days** for one window. Not a grouping key (T6.2, T3.5) |
| Meter readings form one series | Concurrent sessions publish at differing staleness; naive merge showed **+83 points in 1.1 s** of pure artefact (T3.5) |
| Spike attribution keys off the meter curve | It keys off the **ledger's token burn rate**, needs no calibration, and works with no meter at all (T7.6) |
| Codex is a milestone-4 concern | Codex is in **M0** — the episode that prompted this work was on Codex (T0.1b) |

## Repository layout decision

The repository is currently flat top-level scripts. The new code goes into a `usage/` package rather
than more top-level files, because it is a dozen-plus modules with real internal structure and a
shared CLI. Existing scripts stay where they are and keep working; they gain imports from `usage/`
as they are migrated. `statusline.py` in particular must remain a single file that can be copied to
`~/.claude/statusline.py` and run standalone — the README's installation instructions depend on
that, so any `usage/` import it gains must degrade to a reduced status line when the package is not
importable, never fail.

Ticket file paths below assume this layout. An implementing agent must not restructure existing
top-level scripts except where a ticket says so.

---

## E0 — Stateless `why`

The user's question can be answered with one command and no stored state. Ship that first, get it in
front of a real user, and let its output shape the schema rather than the reverse.

### T0.1 — Stateless attribution command
**Implemented:** 2026-09-03 in the shared `why.py` command; awaiting user review of the output shape.
**Files:** `why.py` (new, top level, self-contained)
**Scope:** Read `~/.claude/projects/**/*.jsonl` including `<session>/subagents/agent-*.jsonl` for a
given date or the last N hours. Reduce by `requestId` taking the **maximum per token field** (see
T2.2 — not last-write-wins). Group and rank by: thread kind (main vs subagent, with
`attributionAgent` as the type), `entrypoint` (interactive vs headless/SDK), exact `message.model`,
`effort`, project, and session — each with its share of the window. Print the comparison against the
statusline's own recorded total for the same period so the coverage gap is visible.
**Explicitly not in scope:** persistence, config, sync, pools, meters, energy, cost, other harnesses.
**Acceptance:** run on a real machine, it reproduces the coverage figures in architecture §2 and
names the top contributors for a chosen window; a second run produces identical output; reviewed
with the user before E1 starts.

### T0.1b — Stateless attribution command for Codex
**Implemented:** 2026-09-03 in the shared `why.py` command; awaiting user review of the output shape.
**Files:** `why_codex.py` (new, top level, self-contained) or a `--harness codex` mode of T0.1
**Scope:** Same output shape as T0.1, over `~/.codex/sessions/**/rollout-*.jsonl`. Sum
`payload.info.last_token_usage` **per-turn deltas** — not `total_token_usage`, which is cumulative
and would multiply-count. Deduplicate repeated events whose full `total_token_usage` snapshot is
unchanged; live rollouts emit these during non-call UI/status events, sometimes with a repeated
non-zero `last_token_usage`. Treat a decreasing cumulative snapshot as a new counter segment rather
than lost usage. Group and rank by session, model (`turn_context.payload.model`), provider
(`session_meta.payload.model_provider`), and thread origin from
`session_meta.payload.source.subagent.thread_spawn.{parent_thread_id, depth, agent_nickname}` plus
`thread_source ∈ {user, subagent, automation}`.
**Verified 2026-09-03:** Codex records `turn_context.payload.effort` per turn on the current local
format. The command attributes each subsequent token event to the latest turn context and reports
`unknown` rather than guessing when the field is absent.
**Also verify:** whether the SQLite stores alongside the rollouts are authoritative (see T9.1).
**Acceptance:** summed, deduplicated per-turn deltas reconcile against the terminal cumulative total
of every counter segment in a session; a burst window can be listed with its top contributors;
running twice produces identical output.

### T0.2 — Resolve the billing grain before any schema is frozen
**Files:** a findings note; no production code
**Scope:** `message.usage` contains an `iterations[]` array mirroring the usage fields. Determine
whether it represents multiple **billable** inference passes inside one `requestId`. If it does,
`(provider, request_id)` is the wrong dedup key and every total in this design under-counts.
Compare a session's `iterations` structure against an independent measure of billed usage.
**Acceptance:** a written answer with evidence. **Blocks T1.1.** If the grain is wrong, raise it
immediately rather than proceeding — everything downstream assumes it.

---

## E1 — Ledger foundations

Goal: a durable, idempotent, per-call store that everything else reads and writes.

### T1.1 — Ledger record schema and validation
**Files:** `usage/schema.py` (new), `tests/test_schema.py`
**Gated by T0.2.**
**Scope:** Define **four** record types from architecture §4 — call, **error**, meter reading and
heartbeat — version-tagged `v: 1`. Provide `parse()`/`serialise()` and a strict validator returning
structured errors. Required on the call record and easy to forget:
- `fidelity: int` — **on the record, not the collector.** Define the scale here. Without it a reader
  cannot merge records synced from a machine running collectors it does not have.
- `duration_ms` — nullable; with `ts` it is what makes concurrency computable.
- `tool_sig: [str]` — salted hashes only; the runaway detector cannot work without it.
- `confidence` per field, values `exact | estimated | derived | absent`. **`absent` is not zero.**
- `tokens.reasoning` is a subset of `tokens.output` **in the ledger**, but adapters declare
  `reasoning_semantics: subset | additive` and convert — Gemini is additive and a validator cannot
  detect the mistake, since the wrong value still satisfies `reasoning < output`.

Error records carry the same identity and dimension fields plus `status_code` and `attempt`, and no
token counts.
**Acceptance:** round-trip of a fully populated record of each type is lossless; a record missing any
required field is rejected with a named error; a record whose `reasoning > output` is rejected; a
record whose cache-write splits do not sum to `cache_write` is rejected; unknown extra keys are
preserved on round-trip so a newer producer's fields survive an older reader.
**Out of scope:** any I/O.

### T1.2 — Configuration and machine identity
**Files:** `usage/config.py`, `tests/test_config.py`
**Scope:** Config discovery (`$XDG_CONFIG_HOME/agentmon/config.json`, overridable by env), plus
choosing the **storage root** — not `~/.claude/`, which belongs to one specific harness. Defaults
that work with zero configuration. Stable machine id: **not** the hostname (hostnames leak employer
names), and bound to a boot or hardware identifier so a restored backup, cloned VM or container
image does not produce two live writers claiming one id. Privacy settings `labels: plain|hashed|omit`.

**Credential fingerprinting is opt-in and defaults off.** The first draft required a machine-local
salt *and* identical fingerprints across machines, which is self-contradictory — two machines never
share a first-run local salt unless the salt is synced, and syncing it would put a secret in the sync
root. It also cannot honour "never read a credential's value", since HMAC requires reading it, and on
macOS that can raise a Keychain prompt on first run of a tool advertised as zero-configuration.
**Default to one pool per `(harness, provider)`** and offer fingerprinting only for users who
genuinely run several keys against one provider, deriving the salt from the sync root rather than
locally.
**Acceptance:** first run on a clean `HOME` produces a valid config and a stable machine id; the id
does not change across runs but *does* differ after a simulated clone; no credential is read unless
fingerprinting is explicitly enabled; no credential value is ever written to disk or logs.

### T1.3 — Store: append, read, merge, deduplicate
**Files:** `usage/store.py`, `tests/test_store.py`
**Scope:** Immutable sequence-numbered chunks plus a manifest, under the layout in architecture §7.
Day partition is the **UTC date of `ts`**, so "closed day" means the same thing on every machine.
Reader merges all machine directories and deduplicates on `(provider, id)` by these rules, in order:
1. higher `fidelity` wins, **by coherent field group** — all four token counts together, and the
   cache-write total with its 5m/1h split together. Mixing a total from one source with splits from
   another produces a record that violates its own invariant after validation has already run;
2. equal fidelity resolves by **maximum per token field** — order-independent and idempotent, and
   necessary because a record may have been written from an incomplete read (T2.2);
3. a lower-fidelity record may fill field groups the winner left `absent`, recorded in `confidence`.

**Revalidate the merged record.** Records with `id_synthetic: true` are excluded from cross-machine
merges.
**Acceptance:** appending the same record twice yields one row on read; a record relayed via a third
machine's directory does not double count; merging an OTel-style record (total, no split) with a
transcript record (total plus split) never yields splits that disagree with the total; a record
carrying a placeholder `output` merged with the same id carrying the real value yields the real
value **regardless of merge order**; concurrent appends from two processes lose no records; a
truncated final line is skipped without failing the read.
**Out of scope:** sync transport.

### T1.4 — Derived daily index
**Files:** `usage/index.py`, `tests/test_index.py`
**Scope:** Compact per-day aggregate rebuilt from the ledger, keyed by the dimensions the statusline
and step counter need. Fully derivable — deleting it must be safe. Rebuild is incremental for
closed days.
**Acceptance:** index totals equal a direct ledger aggregation for the same range; deleting and
rebuilding reproduces byte-identical output; reading the index for a statusline render completes in
under 50 ms on a 30-day, 100k-call fixture (about an order of magnitude above measured heavy usage).
**Out of scope:** presentation.

### T1.5 — `doctor` skeleton
**Files:** `usage/cli.py`, `usage/doctor.py`, `tests/test_doctor.py`
**Scope:** CLI entry point with subcommand routing, plus `doctor` reporting collector availability,
last successful collection per collector, ledger size and date range, and config validity. Human and
`--json` output.
**Acceptance:** runs cleanly with no data and says so specifically rather than erroring; `--json`
output validates against a documented shape.

---

## E2 — Claude Code transcript collector

Goal: complete per-call coverage with zero configuration, including subagents and headless runs.

### T2.1 — Incremental scanner with cursor
**Files:** `usage/collectors/claude_code_transcript.py`, `tests/test_cc_transcript.py`
**Scope:** Discover `~/.claude/projects/**/*.jsonl` **including `<session>/subagents/agent-*.jsonl`**.

**Start without a cursor.** A full reparse of the retained corpus takes 0.04 s and retention caps its
size, so the first implementation should simply reparse. Add incrementality only when a real corpus
is measured slow — and if added, the file identity must be `(dev, inode, sha256 of the first 4 KiB)`,
not `(path, size, mtime)`. Transcripts are rewritten by compaction and by temp-file-plus-rename,
which changes the inode while leaving the path and possibly the size intact; resuming at a stale byte
offset then parses from the middle of a record. Sub-second mtime precision also does not survive a
sync round-trip. Prune cursor entries for paths deleted by the 7-day retention, or the cursor grows
without bound.
**Acceptance:** subagent directories are discovered; a rewritten-in-place file of the same size is
detected as changed; re-running produces identical output.
**Out of scope:** field mapping (T2.4).

### T2.2 — Request deduplication by maximum, not last write
**Files:** same as T2.1
**Scope:** Claude Code writes one line per content block of a turn, all sharing one `requestId`.
**They do not carry identical usage** — measured, 199 of 583 multi-line request ids differ, because
early lines hold a placeholder `output_tokens` (2–4 is typical) and only the terminating line has the
real count. A real example progresses `output: 4 → 4 → 254`.

Deduplicate on `requestId`, falling back to `message.id`, taking the **maximum per token field**.
Last-write-wins is wrong here: T2.1's cursor means a scheduled run routinely reads a file mid-turn,
emits the placeholder, and sees the same id again next run — and nothing then guarantees the real
value wins. Maximum is order-independent and idempotent, and is verified safe: on the development
corpus the last line equals the per-field maximum for **694 of 694** request ids.

Skip `message.model == "<synthetic>"` for token accounting and emit it as an **error record**
instead — these are real rate-limit and refusal events and the retry diagnostics need them.
**Acceptance:** a fixture where one turn spans six lines with a growing `output_tokens` emits one
call carrying the **largest** value; feeding the lines in reverse order produces the same result;
feeding the first three lines, then all six, produces the same result as feeding all six once; a
fixture reproducing the 807-lines-to-334-calls ratio collapses correctly; raw summation is
demonstrated in the test to overcount cache reads ~2.2× so the regression stays pinned.
**Out of scope:** cross-file dedup (T1.3 handles it).

### T2.3 — Subagent and thread attribution
**Files:** same as T2.1
**Scope:** Populate `thread`. `kind` from the `subagents/` path segment and `isSidechain`; `id` from
`agentId`; `type` from `attributionAgent`; `parent` by walking `parentUuid` to the spawning entry in
the parent transcript. Subagent entries share the parent's `sessionId` — do not treat them as
separate sessions.
**Acceptance:** on a fixture with a main transcript and two subagent files, all calls carry the same
`session`, subagent calls carry distinct `thread.id` and the correct `thread.type`, and main-thread
calls are `kind: main`; a subagent file whose parent transcript is missing still produces valid
records with `thread.parent: null`.

### T2.4 — Field mapping: knobs, origin, cache split, tools
**Files:** same as T2.1
**Scope:** Map the verified fields in architecture §5.2 — `entrypoint` → `origin`, top-level
`effort`, `usage.speed` → `knobs.fast`, `usage.service_tier`, nested
`cache_creation.ephemeral_{5m,1h}_input_tokens`, `usage.server_tool_use` → `tools`, `cwd` →
`project`, `gitBranch` → `branch`, `message.model` verbatim into `model`. Apply the privacy setting
to labels at write time.
**Acceptance:** a real captured entry maps to a record matching a golden fixture; the 5m and 1h cache
splits sum to `cache_write`; `privacy.labels: hashed` produces stable hashes and no plaintext label
anywhere in the output.

### T2.5 — Schema-drift detection
**Files:** same as T2.1, plus `usage/quality.py`
**Scope:** The transcript format is documented as internal and unstable. Validate each parsed entry
against the expected shape. On an unexpected shape, degrade the affected fields to `absent`, emit a
data-quality event naming the field and the observed `harness_version`, and continue. Never guess a
missing value.
**Acceptance:** a fixture with a renamed usage field produces records with that field `absent` plus
one data-quality event, and does not crash or produce zeros-as-data; `doctor` surfaces the event.

### T2.6 — Backfill and retention guard
**Files:** same as T2.1, `usage/doctor.py`
**Scope:** One-shot import of all transcripts currently on disk. Read `cleanupPeriodDays` from
Claude Code settings and warn in `doctor` when the last successful collection is older than half the
retention period, since transcripts are deleted permanently.
**Acceptance:** backfill is idempotent; the retention warning fires on a fixture with a stale cursor
and names the risk in plain language.

---

## E3 — Statusline: demote to renderer and meter recorder

### T3.0 — Verify that quota readings are account-global
**Files:** none (an experiment plus a note in the architecture document)
**Scope:** The whole residual mechanism (T8.2) assumes a rolling-window percentage read on one
machine reflects consumption on every machine on the account. Test it: with two machines on the same
account, record `rate_limits.seven_day.used_percentage` on both within a few minutes, drive
measurable usage on one, and re-read both. Record the result in the architecture document either
way.
**Acceptance:** the assumption is confirmed or refuted with numbers. **If refuted, T8.2 is cancelled**
and coverage reporting (T7.1) becomes the sole answer to cross-machine drain. Do this before any
work in E8.

### T3.1 — Record meter readings
**Files:** `statusline.py`, `usage/meters.py`, `tests/test_meters.py`
**Scope:** On each fire, append a meter reading from `rate_limits.{five_hour,seven_day}` including
`resets_at`. This is the only documented local read of subscription quota, so it must be robust:
absent `rate_limits` (pre-first-response, or API-key users) is a normal case, not an error.
**Acceptance:** readings are appended without blocking the render; a payload with no `rate_limits`
records nothing and does not error; readings are deduplicated when the payload has not changed.

### T3.2 — Render from the derived index
**Files:** `statusline.py`
**Scope:** Daily, weekly and monthly figures come from the derived index instead of the statusline's
own accumulation. The printed line's format is unchanged.
**Acceptance:** output is byte-identical to the current implementation on a fixture where both
sources agree; render stays under the existing latency budget; a missing index degrades to a
reduced line rather than an error.

### T3.3 — Retire the accumulation path
**Files:** `statusline.py`
**Scope:** Remove `update_daily`'s call-boundary detection, midnight baseline carry-forward and
per-session delta bookkeeping — all of which exist only because the statusline was the sole source.
Gate behind a config flag defaulting to the new path for one release, then delete.
**Depends on:** T2.x complete and drift verified in shadow mode.
**Acceptance:** with the flag off, behaviour is unchanged; with it on, the legacy files stop being
written and no reader breaks.
**Out of scope:** deleting existing user data files.

### T3.5 — Reconstruct a usable meter series from concurrent stale publishers
**Files:** `usage/meters.py`, `tests/test_meter_series.py`
**Scope:** Raw meter readings cannot be concatenated in timestamp order. Measured in Codex rollouts
over three days: **162 distinct `resets_at` values for one weekly window**, nine sessions publishing
simultaneously, and a naive time-ordered merge showing **+83 percentage points in 1.1 seconds** —
entirely artefact, since each session publishes a snapshot of differing staleness.

Build a reconstruction that:
- groups readings **by publishing session**, treats each session's series as independent, and
  reconciles to a "latest known value" rather than interleaving publishers;
- **does not use `resets_at` as a window identifier.** On a rolling window it is a derived "when
  capacity returns" estimate that slides continuously as old usage ages out. An earlier draft assumed
  it was a stable window key; it is not, at least for Codex;
- detects **discontinuities** — a plan change resets the denominator, so the series is genuinely
  broken, not noisy — and segments rather than smoothing across them;
- exposes an explicit "current value and how stale it is", which is what a status line actually needs.

**Acceptance:** on a fixture replaying real interleaved multi-session readings, the reconstructed
series contains none of the artefact jumps present in the naive merge; a simulated plan change is
reported as a segment boundary, not as consumption; a reading older than a configured threshold is
labelled stale rather than presented as current.
**Note:** everything in E8 that consumes the meter depends on this. T7.6 deliberately does not.

### T3.4 — Exact model registry, replacing tier guessing
**Files:** `usage/models.py`, `energy_constants.py`, `tests/test_models.py`
**Scope:** Map model ids to family, tier, energy multiplier and price. Current substring tiering
silently misfiles unknown families as the most expensive tier — measured live, one model family was
being counted as a larger one, and another never appeared at all.

Resolve in three steps rather than by exact match alone: **normalise** (strip a trailing
`-YYYYMMDD` snapshot suffix), then **exact match**, then **family-substring fallback**. Dated and
undated ids are in use simultaneously and new snapshot dates appear without a release of this tool,
so a pure exact-id registry would warn on every future model — trading one silent failure for
permanent noise. Warn only when all three steps fail; then keep the verbatim id, flag it in `doctor`,
and price conservatively **with that choice visible in output**.
**Acceptance:** every model id observed in the fixtures resolves; a *hypothetical future* dated id of
a known family resolves via normalisation without a warning; a genuinely unknown family produces a
record retaining the verbatim id, a `doctor` warning, and a flagged conservative estimate.

---

## E4 — Budget pools and providers

### T4.1 — Pool model and assignment
**Files:** `usage/pools.py`, `tests/test_pools.py`
**Scope:** Pool definitions per architecture §4.2. Assign each call a pool from
`(harness, provider, credential fingerprint)`. Default configuration must infer a sensible single
pool with no user setup.
**Acceptance:** a single-provider user needs no configuration; two API keys on one machine resolve to
two pools; no credential value is read or stored.

### T4.2 — Versioned price table
**Files:** `usage/prices.py`, `data/prices-YYYY-MM-DD.json`, `tests/test_prices.py`
**Scope:** Dated price entries per model and token type, including the distinct 5-minute and 1-hour
cache-write rates. Look-ups resolve by call timestamp so a price change never retroactively rewrites
history. Derived costs are marked `derived`; provider-reported costs are marked `exact`.
**Acceptance:** a call before a price change is priced at the old rate; a model with no price entry
yields `cost_usd: null` rather than zero.

### T4.3 — OpenRouter collector and credit meter
**Files:** `usage/collectors/openrouter.py`
**Scope:** Ingest provider-reported cost and native token counts — available inline on the
completion response as `usage.{cost, prompt_tokens, completion_tokens,
prompt_tokens_details.cached_tokens, completion_tokens_details.reasoning_tokens}`, and after the
fact from `GET /api/v1/generation?id=` which adds `native_tokens_*`, `cache_discount`,
`upstream_inference_cost` and `provider_name`. Read the credit meter from `GET /api/v1/key`
(`limit`, `limit_remaining`, `usage`). Historical backfill via `GET /api/v1/activity`.
Note the `usage: {include: true}` request option is deprecated — usage is returned by default.
**Acceptance:** provider-reported cost lands as `exact` and is never overwritten by the price table;
`native_tokens_*` are preferred over normalised counts when both are present; credit readings
reconcile against summed call costs within a stated tolerance on a fixture.

### T4.4 — API-key spend pools
**Files:** `usage/collectors/provider_usage.py`
**Scope:** Where a provider exposes a usage or cost API the account is entitled to, populate a
`spend` meter; otherwise derive spend from the price table and mark it `derived`. Absence of an API
must be reported as a named limitation, never silently substituted.
**Acceptance:** `doctor` states, per pool, whether spend is authoritative or derived.

---

## E5 — Multi-machine sync

### T5.1 — Sync directory contract
**Files:** `usage/sync/layout.py`, `tests/test_sync_layout.py`
**Scope:** Writer and reader for the `machines/<id>/{calls,meters}/YYYY-MM-DD.jsonl` plus
`heartbeat.json` layout. Closed days are immutable.
**Acceptance:** a reader over three machine directories with overlapping relayed records produces
each call exactly once; an unreadable machine directory is reported and skipped, not fatal.

### T5.2 — rsync/SSH transport, hardened
**Files:** `usage/sync/rsync.py`, replaces `remote_sync.sh`
**Scope:** Host list from config, not hardcoded. Single-instance lock with stale-lock recovery,
bounded connect and transfer timeouts, per-host isolation so one unreachable host cannot stall the
rest, and explicit per-host status. This closes a recorded incident in which scheduled runs
accumulated to roughly 110 overlapping processes with no locking and no timeouts.
**Acceptance:** a second invocation while one is running exits immediately with a clear message; a
host that never responds is abandoned at the timeout and the remaining hosts still complete; a stale
lock from a killed process is recovered; a genuinely missing remote file is distinguished from a
permission error.

### T5.3 — Shared-folder transport
**Files:** `usage/sync/folder.py`
**Scope:** Point the sync root at a directory synchronised by any external mechanism. This is the
zero-setup path for users without SSH between their machines and should be the documented default
recommendation.

Three failure modes this transport must handle by construction, because they cannot be detected after
the fact: an appended file being read mid-write (solved by immutable chunks plus a manifest of size
and hash — readers trust nothing outside the manifest); **conflicted copies**
(`… (host's conflicted copy).jsonl`), which the reader must ignore by name pattern, since real ids
deduplicate but synthetic-id records in a *same-machine* conflicted copy would double count; and
iCloud Drive evicting files to `.icloud` placeholders, where a glob silently returns stubs or nothing
with no error.
**Acceptance:** two machine ids writing into one folder merge correctly; a chunk absent from the
manifest is ignored; a simulated conflicted copy containing synthetic-id records does not inflate
totals; a missing chunk listed in the manifest is reported as a named gap rather than silently
skipped.

### T5.4 — Heartbeats and liveness
**Files:** `usage/sync/heartbeat.py`
**Scope:** Each machine records collectors run, harnesses detected, versions, and last success.
Readers use this to report which machines are reporting and which have gone quiet.
**Acceptance:** a machine whose heartbeat is older than a threshold is reported as silent, with its
last-seen time, in `doctor` and in coverage output.

---

## E6 — Attribution engine and reports

### T6.1 — Group-by engine
**Files:** `usage/aggregate.py`, `tests/test_aggregate.py`
**Scope:** Aggregate the ledger over any subset of the cube dimensions, in any of the three
currencies. Pure functions over records.

**Absence must survive aggregation.** Every measure returns `(value, n_records, n_absent)`, never a
bare number. A harness that does not report a token type contributes `absent`, not zero, and a total
that silently includes it is wrong by exactly the amount it cannot see while printing as though it
were complete. Renderers are *required* to mark any figure with `n_absent > 0` — this is the only
thing that makes the "state the omission rather than under-report silently" requirement in the
collector tickets satisfiable.
**Acceptance:** totals match a naive reference implementation on a fixture; grouping by a dimension
absent from some records produces an explicit `unknown` bucket rather than dropping those records; a
fixture mixing a harness that reports cache writes with one that cannot yields `n_absent > 0` and a
marked total, not a silently low number.

### T6.2 — Report windows
**Files:** `usage/windows.py`
**Depends on T3.5.**
**Scope:** Provide the windows reports are computed over. The first draft said "derive boundaries
from meter `resets_at`"; that is **wrong for rolling windows**, where `resets_at` slides continuously
and produced 162 distinct values in three days for a single window. Default instead to explicit
trailing durations that match the limits in force — trailing 5 hours, trailing 7 days — anchored to
the query time, plus arbitrary user-specified ranges. Use `resets_at` only as *advisory* context in
output ("capacity returns around …"), never as a grouping key.
**Acceptance:** a window spanning midnight is handled correctly; windows are reproducible from the
query time alone without any meter data present; `resets_at` appears only as displayed context.

### T6.3 — `why` command
**Files:** `usage/cli.py`, `usage/report_why.py`
**Scope:** The flagship. For a chosen window and pool: total burn, ranked contributors by machine,
session, agent, model and project, concurrency over time, the automation share, the explained share,
and any findings. Human and `--json` output.

**This supersedes `advisor.py --breakdown`**, which already implements a quota breakdown by project
and model with top sessions by estimated quota impact. Migrate its rules into the §9 rule set and
delete the old path — do not leave two commands answering the same question differently.
**Acceptance:** on the recorded busy-day fixture, output attributes the subagent and headless share
correctly and names the top contributors; every figure derived from a partially-`absent` measure is
marked; on an empty ledger it explains that nothing was collected and what to do about it.

### T6.4 — `report` and self-contained HTML
**Files:** `usage/report.py`
**Scope:** Periodic summary; `--html` writes one self-contained file with inline SVG, no external
requests and no server. Redacts labels by default when writing HTML.
**Acceptance:** the HTML opens offline with no network requests; it is readable in both light and
dark colour schemes; redaction is on by default and requires an explicit flag to disable.

### T6.5 — `watch`
**Scope:** Live burn rate and projected time to exhaustion for the active window.
**Acceptance:** projection is labelled as a linear extrapolation; with no meter data it reports token
rate only and says why.

### T6.6 — Statusline burn-rate segment
**Files:** `statusline.py`
**Scope:** Optional additional segment showing window percentage, rate of change and projected
exhaustion. Off by default; the existing line's format is unchanged when disabled.
**Acceptance:** enabling it adds exactly one segment; render latency stays within budget.

---

## E7 — Diagnostics

Each rule emits a structured finding with evidence; renderers share one implementation.

### T7.1 — Coverage and liveness
**Scope:** Which machines and collectors reported into the window, what share each explains, what is
unexplained, and which known machines are silent. This is the always-available answer to "is
something draining my tokens" and must never depend on calibration.
**Acceptance:** a fixture with one silent machine names that machine and its last-seen time.

### T7.2 — Automation share
**Scope:** Burn by `origin`, separating interactive from headless and SDK-driven work.
**Acceptance:** correctly reports the measured case where non-interactive runs were the majority of
volume.

### T7.3 — Runaway detection
**Depends on `tool_sig` and the error record type from T1.1.** Without both, only the rate signal is
computable — in that case descope the ticket explicitly rather than shipping a rule that appears
complete and silently checks one condition.
**Scope:** Flag sessions with a sustained call rate above their own baseline; repeated `tool_sig`
hashes; cache read per call flat and high while output per call collapses; and retry storms from
error records. Prefer `duration_ms`-based concurrency over completion-timestamp rate, which conflates
queueing with burn. Store only salted hashes, never tool inputs.
**Acceptance:** a synthetic looping fixture is flagged; a legitimately busy session is not; a
retry-storm fixture is distinguished from productive burn; no plaintext tool input appears in any
output or stored artefact.

### T7.4 — Knob impact
**Scope:** Burn per call grouped by effort, fast mode and thinking, holding model and project fixed.
**Acceptance:** reports a ratio with sample sizes and withholds a claim when a group is too small.

### T7.5 — Model counterfactual
**Scope:** Re-price a window under a substitution policy to estimate the saving from routing some
work to a smaller model.
**Acceptance:** clearly labelled as an estimate assuming identical token volume; states that
assumption in the output rather than only in the docs.

### T7.6 — Spike attribution
**Scope:** Find the steepest segment of the **ledger's own token burn rate** — not the meter curve —
and report everything running during it, ranked. The meter is quantized, non-monotone, discontinuous
across plan changes and corrupted by concurrent stale publishers (T3.5); the ledger's token rate is
clean and at per-call resolution.

This is the highest-value rule in the set and it needs **no calibration whatsoever**: for a
concentrated burst, "here is everything that ran between 14:02 and 14:31, ranked by tokens" is a
complete answer to "what caused that". Build it early — it is a candidate to fold into M0.
**Acceptance:** on a fixture with a sharp burn, the responsible sessions rank first; the rule
produces the same result with the meter series entirely absent.

---

## E8 — Reconciliation

### T8.1 — Explained versus observed, currency meters
**Scope:** For `credit_balance` and `spend` pools, compare meter movement to summed call cost and
report the residual.
**Acceptance:** residual is zero within tolerance on a consistent fixture; a deliberately incomplete
fixture produces a residual of the expected magnitude.

### T8.2 — Coarse quota cross-check (single-scalar, heavily gated)
**Gated by T3.0.** Do not start until the account-global assumption is confirmed; if T3.0 refutes it,
close as cancelled.
**Scope:** The four-way regression in the first draft is **cancelled**. Measurement showed the meter
decreases without a window reset (214 times on the 5-hour window, 42 on the 7-day window, over 3.6
days) because the windows roll and old burn expires; that `used_percentage` is quantized to whole
integers, so per-interval change is 0 or ±1; and that the four token types move near-proportionally,
so a four-way fit returns opposite-signed coefficients fitted to noise. The failure mode is a
confident wrong answer — a negative cache-read weight yields negative explained burn and the tool
announces that most of the week went to an invisible machine when nothing else ran.

Instead: fix the relative weights to published price ratios (`advisor.py`'s existing
`i + o*5 + c*0.1 + cw*1.25` is exactly this) and fit a **single scalar** mapping weighted tokens to
percentage points. Restrict to admissible intervals only — no observed decrease, and at least three
points of change so quantization does not dominate. Report an interval, never a point. Hard-fail on a
negative or implausible scale rather than publishing it. **Never claim per-model quota attribution**:
the separate limit for the largest model is not observable anywhere.
**Acceptance:** the synthetic-data test must include non-monotonicity, integer quantization and
collinear inputs — a fixture without them proves nothing; on data resembling the real meter the
system declines to publish and says why; a negative fitted scale is refused, not reported.

### T8.3 — Residual reporting
**Scope:** Surface residual in `why` and `doctor` with its confidence band and the coverage context
needed to interpret it.
**Acceptance:** output never presents a residual without its fit quality and the list of contributing
machines.

---

## E9 — Other harnesses

### T9.1 — Codex collector (ledger-backed)
**Note:** the stateless Codex reader is **T0.1b in M0**; this ticket promotes it to a ledger
collector and should reuse its parsing rather than reimplement it.
**Scope:** Map rollout records to ledger records from
`payload.info.last_token_usage.{input_tokens, cached_input_tokens, cache_write_input_tokens,
output_tokens, reasoning_output_tokens}` — use the per-turn delta, not the cumulative total, to
avoid double counting. Reasoning is a subset of output. Populate `thread` and `origin` from
`session_meta.payload.source.subagent.{parent_thread_id, depth, agent_nickname, agent_role}` and
`thread_source ∈ {user, subagent, automation}`. Feed `payload.rate_limits.{primary, secondary}` into
a `rolling_pct` meter using `used_percent` and `resets_at`, and `credits.balance` into a credit meter
on the same pool. **Identify each window by its `window_minutes` value, never by whether it appeared
in `primary` or `secondary`** — a live rollout inspected during design carried the weekly window
(`window_minutes: 10080`) in `primary` with `secondary: null`, so position-based reading would label
weekly consumption as five-hour consumption.
**First verify which store is authoritative.** SQLite databases are actively written alongside the
JSONL rollouts and their role is unverified; do not assume the existing parser's assumptions hold.
**Note:** this repository's `research/multi-cli-support.md` states Codex exposes no cache-write
tracking. That is now **out of date** — the field exists. Correct that document as part of this
ticket.
**Acceptance:** a fixture rollout produces correct records; summing per-turn deltas equals the
final cumulative total; subagent turns are attributed to the spawning thread; the weekly rate-limit
field being null before first use is handled as absent rather than zero.

### T9.2 — Pi collector
**Scope:** Map normalised per-response usage. Pi is multi-provider, so `provider` and `pool` come
from the record, not from the harness. Preserve the existing fork/clone deduplication behaviour.
**Acceptance:** forked sessions do not double count; provider is populated per call.

### T9.3 — Collector contract documentation and template
**Scope:** Document the interface and ship a template plus a conformance test suite any new
collector can run.
**Acceptance:** a third party can add a harness without modifying core modules; the conformance suite
catches a collector that emits duplicate ids or additive reasoning tokens.

---

## E10 — OpenTelemetry high-fidelity path

### T10.0 — Confirm the transport is viable before building the epic
**Scope:** Verify that the **logs** exporter supports `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`. The
entire epic assumes a stdlib receiver can parse what is sent; if only protobuf is offered, a
stdlib-only receiver is a far larger undertaking and E10 needs rethinking.
**Acceptance:** a written yes/no with a captured payload. **Blocks T10.1.**

### T10.1 — Loopback OTLP receiver
**Files:** `usage/otel_receiver.py`
**Scope:** Standard-library HTTP server accepting OTLP on loopback only, writing to the ledger. The
only long-running component in the system, strictly opt-in. Do **not** bind 4318 — it is the OTLP/HTTP
default and is frequently already in use; take the port from config and **fail loudly on a bind
conflict** rather than appearing to run while receiving nothing.
**Acceptance:** binds `127.0.0.1` only and refuses remote connections; a bind conflict is a visible
error, not a silent no-op; malformed payloads are rejected without crashing; the process survives a
client disconnecting mid-request.

### T10.2 — Map OTel records to the ledger
**Scope:** Consume the **`api_request` log event** — model, all four token types, cost, duration,
request id, effort, speed, query source. Map `api_error` to error records.

**Do not ingest `claude_code.token.usage`.** It is a cumulative counter exported on an interval and
carries **no request id**; treating its datapoints as ledger rows double-counts. Metrics are at most
a cross-check.
**Acceptance:** records deduplicate correctly against transcript-derived records for the same request
id and win on fidelity; a test feeds counter datapoints and asserts they are *not* ingested as calls.

### T10.3 — Setup documentation
**Scope:** Document the environment variables, per-platform service installation, and cardinality
controls. Two things must be explicit: configure the **per-signal**
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, never the global `OTEL_EXPORTER_OTLP_ENDPOINT`, which would
silently disable any collector the user already runs — and anyone willing to enable telemetry is
disproportionately likely to have one; and OTel is strictly **additive**, with the transcript
collector still running to backfill receiver downtime, because exporters buffer in memory and then
drop, losing data on the highest-fidelity source.

---

## E11 — Public-release readiness

### T11.1 — Neutral naming and compatibility shims
**Scope:** `pi_scanner.py` and the `pi_*` data filenames mean "headless scanner", not "Raspberry Pi",
and confuse every reader. Rename to neutral names, keep shims for one release, document the
migration.
**Acceptance:** old paths keep working with a deprecation notice; no data is moved without a
reversible migration step.

### T11.2 — Remove environment-specific defaults
**Scope:** Host lists, machine names and thresholds move to configuration with generic defaults. No
personal hostname or path may remain in the shipped code. Three items the obvious sweep misses:
- **Legacy machine ids are hostnames.** The existing journals default their machine id to
  `socket.gethostname()`, so the history import in migration step 3 would inject hostnames into a
  store defined not to contain them, and make one physical machine appear as two in every
  machine-grouped report. Ship a legacy-id → machine-id mapping.
- **The storage root is another tool's directory.** Everything currently lives in `~/.claude/`. A
  Codex-only user of a harness-agnostic tool should not get state there.
- **Repository contents.** Several tracked files are personal or unrelated to the tool — the session
  state file, harness persona configuration, draft posts and one-off analysis scripts. Decide
  deliberately what a public repository should ship; this is a judgement call for the maintainer, not
  something to remove unilaterally.
**Acceptance:** a grep for the maintainer's machine names and paths returns nothing outside
historical documentation; a fresh install on an unrelated machine needs no code edits; imported
legacy history maps to the same machine id as that machine's live records.

### T11.3 — Correct the stale token-accounting findings
**Files:** `FINDINGS.md`, `README.md`
**Scope:** Two corrections. First, the documented "transcript output excludes thinking, expect ~3×"
gap **no longer holds** on current versions — measured within 1% on a full day — so any hard-coded
correction factor must go and calibration must be measured at runtime. Second, document the
`requestId` deduplication requirement and the content-block fan-out that causes roughly 2.2×
overcounting without it. Both are prerequisites for anyone trusting the numbers.
**Acceptance:** claims in the docs match what the current code measures; each claim states the
version it was verified against.

### T11.4 — Documentation
**Scope:** README section for the attribution feature; the architecture document linked; a worked
example of `why`; an honest limitations section covering unobservable per-model quota limits, the
unstable transcript format, and calibration caveats.

### T11.5 — Test suite and CI
**Scope:** Consolidate the ad-hoc test scripts into one runner. Fixtures for every collector. CI on
supported Python versions.
**Acceptance:** one command runs everything; the suite passes from a clean checkout with no network
access and no personal data.

### T11.6 — Packaging
**Scope:** Single CLI entry point and installable package, so the multi-file layout does not have to
be cloned and wired up by hand.
**Acceptance:** installing and running `doctor` on a clean machine works without cloning.

---

## Cross-cutting requirements

Apply to every ticket:

- **No prompt or tool-input content is ever persisted.** Hashes only, salted.
- **No credential value is read for its content.** Fingerprints only.
- **Nothing silently guesses.** An unknown model, a drifted schema, a missing meter and an
  unrecognised harness are all reported as named gaps.
- **`absent` is never zero.** Aggregates carry `n_absent` per measure and renderers must mark any
  figure computed from an incomplete one. A number that is quietly low is worse than a number that
  admits what it is missing.
- **Merges must be idempotent and order-independent.** Any rule where processing the same records
  twice, or in a different order, changes the result is a bug — collectors re-read, syncs redeliver,
  and files get read mid-write.
- **No aggregate becomes a source of truth.** If a number cannot be rederived from the ledger, it is
  a bug.
- **Backwards compatibility.** No existing user data file is deleted or rewritten in place; readers
  tolerate records from newer producers.
