# Local observed-usage history (issue #10)

## T0.2: request identity is not a billing grain

Decision, 2026-09-26: keep **request observations**, not "one verified billable
inference per request". The v1 SQLite format is an observed-usage schema; it is
not the billing schema proposed in the earlier architecture document. Each
observation preserves allowlisted raw counters and `iterations` (including
iteration type/model), source/collector versions and provenance. When streaming
arrays grow or change order, original sanitized `iteration_snapshots` are retained;
incompatible type/model positions are never merged. The representative `iterations`
array may contain derived maxima; `iteration_snapshots` contains only original
sanitized arrays. Layout changes are warnings. Reports set
`billing_verified: false` and never attach an invoice amount.

Anthropic's [advisor documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool#usage-and-billing),
checked 2026-09-26, states that advisor iterations can use another model and
are excluded from the top-level executor counters. Therefore `(provider,
request_id)` identifies an envelope, not necessarily one billed inference.
Adding the envelope and its iterations would also double-count overlapping
executor work. This is sufficient to reject the old universal billing-grain
assumption; it does not independently verify a customer's bill.

A local structural check of retained Claude Code transcripts on 2026-09-26
(versions 2.1.270 through 2.1.283) found populated iteration arrays containing
one iteration, with each of the four counters equal to the parent counters.
Empty/null arrays also occurred. These were streaming rows, not independently
deduplicated paid requests. There was no independent invoice or API usage
reconciliation, and no paid model calls were made for this investigation.

The importer counts top-level observed usage once. Multiple iterations,
non-message iterations or mismatching single-iteration counters mark the
observation incomplete. Reports expose a **known subtotal** and set complete
total to null in such cases; the retained iterations permit later, explicit
reprocessing. They are not silently discarded or added to the parent.
`known_tokens` excludes observations with ambiguous synthetic identity. Their
counters appear separately in `ambiguous_identity_tokens` and
`ambiguous_identity_token_fields`; these may overlap and are not additive to the
known subtotal. Any ambiguous identity makes complete totals null.
No number in this feature claims full-account coverage or verified billing.
Before issue #12 prices such records, it must implement versioned iteration
normalization and model/rate attribution, with independent billing evidence
where an exact charge is claimed.

## Storage decision

For the single-machine seminar MVP, SQLite from the Python standard library
provides transactions, a writer lock, deduplication and file checkpoints in one
local file. This intentionally implements a smaller slice than the earlier
immutable-chunk/multi-machine design. No sync, quota meter, heartbeat service,
credential fingerprint, or statusline migration is introduced.

`PRAGMA user_version=1` versions the database. Unknown database versions fail
closed; later migrations must be explicit and preserve original observations.
On POSIX systems, database files are 0600 and a newly created immediate state
directory is 0700. Native Windows uses the current user's filesystem ACLs because
POSIX ownership and mode checks are unavailable.
A random machine ID persists in the database and does not contain a hostname.
It identifies this local store, not physical hardware; cloned stores are not
independent machine identities. Cross-machine merging is outside v1 scope.

The four normalized token categories are non-overlapping. Codex fresh input
requires all of its input/cache counters; if a cache field is absent, the
remainder is unknown. Pi already stores reasoning as a subset of output. OpenCode
stores text output and reasoning separately, so its adapter adds reasoning to the
normalized output while retaining the reasoning subtotal. Reports therefore always
treat reasoning as a subset of normalized output and never add it again.
Unavailable fields remain null. Raw counters survive normalization;
nontrivial iterations and inconsistent token relationships are warnings.

Only changed source files are reparsed (whole file, to retain session context).
OpenCode is read through a query-only SQLite connection; its database, WAL, and SHM
metadata form the checkpoint fingerprint. Other file fingerprints include inode,
size, modification and change times. Source
checkpoints and merged observations commit atomically under SQLite's writer
lock. The same provider/harness/request key is merged with maximum observed
counters, so streaming updates cannot regress. Codex events without an ordinal
use the session, timestamp, cumulative counters and reset segment, independent of
physical line numbers. Claude row UUIDs without a request/message ID and Codex
rows lacking enough identity metadata are explicitly ambiguous. Pi response IDs
are provider-scoped; copied fork entries retain the earliest source session.
OpenCode message IDs are the per-call identity and sessions are scoped by harness
in the HTML report. Synthetic identities are scoped
to this store's machine ID and have weaker guarantees. No data is deleted from
history because a source is rotated, truncated or removed. Errors and partial
tails are visible; incomplete tails are reparsed on the next refresh.

Imports are manual. Run `refresh` before source retention expires; an optional
scheduler can be configured separately. There is no background collector or
installation change in this ticket. Old daily aggregates cannot reconstruct
missing request, prompt or minute-level data and are not added to transcripts.

## Privacy and interpretation

Prompt content, tool inputs, credentials and environment configuration are
never persisted. User turn IDs are linked from observed metadata where possible;
sequential associations are marked derived. Unlinked turns/subagents remain
explicitly unknown. A prompt reference is not a copy of the prompt.

The local JSON report includes project identities and import root paths;
`--records` additionally includes individual source-file references;
keep it private. The default HTML report pseudonymizes direct identities but retains
exact times and model metadata, so it is not guaranteed anonymous. An event's timestamp
is an attribution time, not a measurement of continuous token generation during
that minute. Request duration is null when unavailable. Local-day/hour/minute
buckets use IANA timezone rules and distinguish repeated DST hours by UTC offset.
