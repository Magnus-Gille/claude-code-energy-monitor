# Project Status

**Last session:** 2026-03-02
**Branch:** master

## Completed This Session
- Researched Gemini CLI and Codex CLI token tracking architectures
- Assessed packaging options (PyPI vs npm vs Homebrew vs status quo)
- Documented findings in `research/multi-cli-support.md`

## Key Findings
- **Codex CLI:** Easy integration via JSONL rollout files at `~/.codex/sessions/`
- **Gemini CLI:** Moderate — OTel telemetry (opt-in) or parse raw activity logs
- **Neither has cache-write tracking** (only Claude Code does)
- **Packaging:** PyPI via `pipx` is the right choice if/when multi-CLI lands; premature now

## In Progress
- Nothing active

## Next Steps
- Per-model energy constants (Haiku/Sonnet/Opus use same constants despite ~2-5x size differences)
- Consider blog post about JSONL placeholder finding (issue #28197)
- Context-length-dependent input constants
- Multi-CLI support (Codex first, then Gemini) — if pursuing, start with shared storage layer
- PyPI packaging — defer until multi-CLI or broader adoption
