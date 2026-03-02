# Multi-CLI Support Research

**Date:** 2026-03-02
**Status:** Research only — no implementation

## Goal

Evaluate extending the energy monitor to support Gemini CLI and OpenAI Codex CLI alongside Claude Code.

## Codex CLI — Easy

**Architecture:** Rust (96%), Ratatui TUI. No external statusbar process.

**Token data access:** Session rollout JSONL files at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Written in real-time, can be tailed. Each `token_count` event contains cumulative `total_token_usage` and per-turn `last_token_usage`.

**Token fields:**
- `input_tokens` (total input, includes cached)
- `cached_input_tokens` (subset served from cache)
- `output_tokens` (includes reasoning)
- `reasoning_output_tokens` (subset that was reasoning)
- `total_tokens`

**What's missing vs Claude Code:**
- No cache-write tracking (only reads)
- No cross-session persistent storage — we'd build that ourselves
- `--ephemeral` flag suppresses rollout files

**Existing ecosystem:** `ccusage`, `codex-hud`, `tokscale`, `CodexBar` all parse the rollout JSONL successfully.

**Verdict:** Straightforward integration. Parse JSONL, accumulate into our daily storage format.

## Gemini CLI — Moderate

**Architecture:** TypeScript/Node.js, Ink TUI. Token data lives in-memory in `UiTelemetryService` EventEmitter.

**Token data access:** Two paths:
1. **OpenTelemetry local file** (opt-in via `.gemini/settings.json`): Structured metrics including `gemini_cli.token.usage` counters with `{model, type}` attributes. Type = `input|output|thought|cache|tool`. Format is pretty-printed JSON (NOT JSONL).
2. **Activity logger JSONL** at `<projectTempLogsDir>/session-<id>.jsonl`: Raw HTTP request/response bodies including `usageMetadata`. Always written.

**Token fields (from API):**
- `input_token_count` (promptTokenCount)
- `output_token_count` (candidatesTokenCount)
- `cached_content_token_count` (cache reads)
- `thoughts_token_count` (reasoning)
- `tool_token_count` (tool-related prompt tokens)
- `total_token_count`

**What's missing vs Claude Code:**
- No cache-write tracking
- No cross-session persistent storage
- OTel telemetry disabled by default (user must enable)
- No statusbar stdin stream ([#8191](https://github.com/google-gemini/gemini-cli/issues/8191) still open)
- Activity logger requires parsing raw API response bodies

**Verdict:** Doable but user needs to enable OTel, or we parse raw activity logs.

## Comparison Table

| Aspect | Claude Code | Codex CLI | Gemini CLI |
|--------|------------|-----------|------------|
| Data access | Statusbar stdin JSON | JSONL rollout files | OTel file (opt-in) or activity JSONL |
| Always available? | Yes | Yes (unless `--ephemeral`) | Activity logger: yes. OTel: opt-in |
| Cache read | Yes | Yes | Yes |
| Cache write | Yes | No | No |
| Reasoning tokens | Bundled in output | Separate field | Separate field |
| Cross-session storage | Built-in | None | None |

## Architectural Implications

**What changes if we go multi-CLI:**

1. **Shared accumulation layer** — The daily JSON + history JSONL logic becomes shared code with per-CLI parsers feeding into it.

2. **File watching vs stdin** — Both Codex and Gemini use file output, not stdin streams. Need `watchdog` or periodic reads for real-time monitoring. The statusline approach (stdin reader) wouldn't work for these.

3. **Per-model energy constants** — Each CLI uses different model families with different energy profiles. Need a constants table keyed by model name/family, not just one set of constants.

4. **Unified token schema** — Different field names and semantics need normalization:
   - Cache write: only Claude Code has it
   - Reasoning tokens: Codex/Gemini separate them, Claude Code doesn't
   - Input tokens: Codex includes cached in total, Claude Code excludes cached from `total_input_tokens`

5. **Language choice** — Python remains fine. No reason to change.

6. **Packaging** — Multi-CLI support makes PyPI packaging (`pipx install`) more justified than the current git-clone workflow. npm would be awkward (Python script in npm wrapper).

## Packaging Assessment

| Option | Fit | Notes |
|--------|-----|-------|
| PyPI (`pipx install`) | Best | Natural for Python; becomes justified with multi-CLI |
| npm | Poor | Wrapping Python in npm is awkward |
| Homebrew | Decent | macOS-only, tap maintenance overhead |
| Status quo (git clone) | Fine for now | Single script, tiny audience |

**Recommendation:** Package via PyPI if/when multi-CLI support is implemented. Until then, git clone is adequate.

## Key Sources

- Gemini CLI repo: https://github.com/google-gemini/gemini-cli
- Gemini CLI telemetry docs: https://google-gemini.github.io/gemini-cli/docs/cli/telemetry.html
- Gemini statusline feature request: https://github.com/google-gemini/gemini-cli/issues/8191
- Codex CLI repo: https://github.com/openai/codex
- ccusage Codex guide: https://ccusage.com/guide/codex/
- codex-hud (real-time rollout watcher): https://github.com/fwyc0573/codex-hud
