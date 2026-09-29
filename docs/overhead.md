# Fixed context overhead

`energy-monitor overhead [--refresh] [--harness H] [--since ISO] [--json]` shows how much fixed context a
session carries: the system prompt, tool lists, instruction files (AGENTS.md/CLAUDE.md), the skills list, MCP
instructions, and skill bodies when a skill is used. `--refresh` rescans the default session roots first.

## What is exact and what is estimated

| Harness | Exact | Estimated / unavailable |
| --- | --- | --- |
| Claude Code | Character counts of `attachment` rows (skill listing, each instruction file, nested memory, MCP instructions, deferred tools, agent listing, system prompt snapshot, session context); skill body size from the `isMeta` row that follows a `Skill` tool use; floor tokens from the first assistant row (`input + cache_creation + cache_read`) | Tokens of components (4 characters per token) |
| Codex | System prompt (`base_instructions`), `<skills_instructions>`, `<INSTRUCTIONS>` and `<environment_context>` blocks (tags included, each block separately); floor from the first `token_count` `last_token_usage.input_tokens` | Skill reads are `heuristic`: an `exec` call that mentions a path ending in `SKILL.md`, sized by its output |
| Pi | Floor only, from the first assistant message with nonzero usage (`input + cacheRead + cacheWrite`) | Components unavailable |
| OpenCode | Skill output size (`skill` tool parts); floor from the first assistant message tokens (`input + cache.read + cache.write`) | Components unavailable |

The floor is the whole first request, so it also contains tools, the first user prompt and anything the
harness does not log. The report shows `other: tools, first prompt, unlogged` as floor minus the estimated
component tokens (median over sessions that log components). It is an estimate, not a measurement.

## Recurring cost

The floor is re-read on every call after the first, so the estimate is `floor x (calls - 1) x cache_read price`,
labelled `API-equivalent at list price, estimate`. The price comes from the session's dominant model in the
history database (`usage.pricing`, priced per call so long-context tiers use the per-call size); a session with
no history records, or an unpriced model, contributes no cost and is counted as unpriced. Subagent sessions use
their parent's dominant model. `calls` counts distinct priced requests: Claude `requestId` (else message id),
Codex distinct cumulative token totals, Pi `responseId` (else entry id), OpenCode assistant messages with tokens.
Cache writes on the first call are not included, and a cache miss or expiry mid-session would raise the real bill.

## The 4 characters per token estimate

Component tokens are `characters / 4`. That is a rough English-prose figure; code, JSON, non-English text and
tokenizers differ (often 2.5 to 3.5 characters per token for code), so treat token figures as order of magnitude.
Character counts and floor tokens are the only measured values.

## Privacy

Stored in the history database (tables `overhead_sessions`, `overhead_items`, `overhead_skill_uses`): sizes in
characters, component and skill names, counts, session ids, timestamps and token counts. Never content, prompts,
tool inputs or arguments. Instruction-file and nested-memory paths are stored only as `basename#hash` where the
hash is the first 10 hex digits of SHA-256 of the full path. Rescanning is rescan-all with an upsert per
harness and session, so repeated runs are idempotent; sessions that later disappear from disk are retained.
