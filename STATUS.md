# Project Status

**Last session:** 2026-03-27
**Branch:** master

## Completed This Session
- Wrote [`debate/heimdall-env-codex-rebuttal-1.md`](debate/heimdall-env-codex-rebuttal-1.md), a Round 2 rebuttal to Claude's revised Heimdall `EnvironmentFile` plan
- Accepted that Claude made genuine concessions on root-cause certainty, March 22 speculation, and the draft's internal inconsistency
- Concluded the revised plan is close but still insufficient as written for incident close-out: it needs `daemon-reload` and explicit verification of intended account selection, not just a fallback check after failure
- Wrote [`debate/heimdall-env-codex-critique.md`](debate/heimdall-env-codex-critique.md), an adversarial review of Claude's proposed Heimdall `EnvironmentFile=/home/magnus/.heimdall/env` fix
- Concluded the `EnvironmentFile` change is a reasonable minimal deployment fix but not a proven root-cause resolution
- Flagged the main evidentiary gaps: the diagnosis overstates certainty from one env-var mismatch, the two-account MSAL cache remains a live secondary risk, and the March 22 explanation is speculative
- Wrote [`debate/model-tiering-codex-rebuttal-1.md`](debate/model-tiering-codex-rebuttal-1.md), a Round 2 rebuttal to Claude's revised model-tiering position
- Accepted that Claude made genuine concessions on sequencing, speculative quota math, and custom-scheduler complexity
- Argued that the remaining blocker is operational: Hugin still needs a minimal invocation journal and explicit acceptance contract before any broad Sonnet-first rollout
- Wrote [`debate/model-tiering-codex-critique.md`](debate/model-tiering-codex-critique.md), an adversarial review of Claude's model-tiering proposal for Hugin
- Concluded that the core premise is directionally valid because Anthropic docs indicate model choice affects usage limits, but the draft's concrete Max-quota savings math is unproven
- Recommended a simpler rollout order for Hugin: Sonnet-first with acceptance-check-based escalation and telemetry first, planner/scheduler second
- Wrote [`debate/headless-energy-codex-critique.md`](debate/headless-energy-codex-critique.md), an adversarial review of Claude's headless-energy proposal
- Wrote [`debate/headless-energy-codex-rebuttal-1.md`](debate/headless-energy-codex-rebuttal-1.md), a Round 2 rebuttal after Claude's empirical response
- Acknowledged that Claude empirically resolved the top `--resume` overcount concern and that journal-first wrapper architecture is now directionally acceptable
- Flagged the remaining blocker as thinking-token semantic validation, plus an internal inconsistency in Claude's reported `output_tokens` vs claimed ratio
- Critiqued `--output-format json` reliability, including schema-contract uncertainty, error/interrupt edge cases, stdout compatibility, and resumed-session overcount risk
- Assessed alternatives (hooks, plugins, file watchers, API-level integration, cost-only derivation) and argued that hooks help as triggers but not as an accurate token source
- Called out Pi-specific constraints and multi-turn headless agent-session risks, especially the unresolved question of invocation-delta vs resumed-session-cumulative usage
- Implemented `codex_status.py`, a Codex companion monitor that parses `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Added rollout-summary caching in `~/.codex/statusline_rollout_cache.json` to keep repeated prompt/status invocations under 1s
- Matched Codex token semantics correctly: fresh input = `input_tokens - cached_input_tokens`; output includes reasoning; cache write unavailable and treated as zero
- Rendered Claude-style status segments for Codex: model, context window, 5h/7d quota (when present), and D/W/M token + energy totals
- Implemented `codex_stepcount.py` for copy/pasteable Codex usage summaries (`-d/-w/-m/-t/--rough-energy-estimate/--copy`)
- Implemented `codex_with_summary.py` wrapper to run `codex` and print the Codex summary on exit, since Codex still has no native stop hook
- Verified the script against real local Codex rollout files and specific historical files with rate-limit data
- Updated `README.md` with Codex usage/integration examples and refreshed `research/multi-cli-support.md` to reflect implementation

## In Progress
- Nothing active

## Next Steps
- **Heimdall env-file debate** (Round 2 completed 2026-03-27):
  - Treat `EnvironmentFile + verify` as an immediate repair attempt, not full closure
  - Include `systemctl daemon-reload` if the unit file or drop-in is changed
  - Verify not just endpoint success but the actual account/mailbox selected by Heimdall
- **Model tiering for Hugin** (debate Round 2 completed 2026-03-26):
  - If implemented, do not flip all code tasks to Sonnet by default without telemetry
  - First add a minimal append-only invocation journal from the first pilot run
  - Define a narrow acceptance contract for a small pilot task set before broader Sonnet-first rollout
  - Treat `opusplan` as research only until Sonnet-first pilot data shows a planning gap
- **Pi headless energy monitoring** (debate + verification done 2026-03-21):
  - Build a JSONL scanner that reads print-mode session files from `~/.claude/projects/<path>/*.jsonl` on the Pi
  - Print-mode JSONL is clean: 5 lines per session, one assistant entry with accurate usage (no placeholders, no streaming duplicates — unlike interactive JSONL)
  - No changes to Hugin needed — Hugin uses `-p` which already writes clean JSONL
  - Extended thinking is DISABLED in print mode, so `output_tokens` = visible output only (accurate for actual compute)
  - `--resume` returns per-invocation usage, not cumulative (verified)
  - Architecture: append-only invocation journal → derived daily rollups, with machine_id + claude version stamps
  - Add `machine` field to history entries for cross-machine aggregation with laptop data
  - See `debate/headless-energy-summary.md` for full debate results
- Per-model energy constants (Haiku/Sonnet/Opus use same constants despite ~2-5x size differences)
- Improve active-session heuristics if Codex changes rollout behavior or adds a native status hook
- Consider blog post about JSONL placeholder finding (issue #28197)
- Context-length-dependent input constants
- Gemini CLI support — still research only
- PyPI packaging — defer until multi-CLI or broader adoption
