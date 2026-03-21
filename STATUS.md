# Project Status

**Last session:** 2026-03-21
**Branch:** master

## Completed This Session
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
