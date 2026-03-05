# Project Status

**Last session:** 2026-03-05
**Branch:** master

## Completed This Session
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
- Per-model energy constants (Haiku/Sonnet/Opus use same constants despite ~2-5x size differences)
- Improve active-session heuristics if Codex changes rollout behavior or adds a native status hook
- Consider blog post about JSONL placeholder finding (issue #28197)
- Context-length-dependent input constants
- Gemini CLI support — still research only
- PyPI packaging — defer until multi-CLI or broader adoption
