# Multi-CLI Support Research

**Date:** 2026-03-02
**Status:** Codex implemented on 2026-03-05 via `codex_status.py`; Pi coding harness implemented on 2026-07-16 via `pi_status.py`; Gemini remains research only

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
- ~~No cache-write tracking (only reads)~~ — **corrected 2026-08-10, see below**
- No built-in statusline hook inside the Codex TUI
- `--ephemeral` flag suppresses rollout files

**Correction (2026-08-10, verified against a live rollout file):** Codex now records cache writes and
sub-agent structure. `payload.info.{last,total}_token_usage` contains
`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, `total_tokens` — so the "no cache-write tracking" limitation above, and
the comparison table's "Cache write: No" row for Codex, are both out of date. Rollouts also carry
`payload.rate_limits.{primary,secondary}` (`used_percent`, `window_minutes`, `resets_at`),
`credits.{has_credits,unlimited,balance}`, `session_meta.payload.model_provider`, and
`session_meta.payload.source.subagent.thread_spawn.{parent_thread_id,depth,agent_nickname}` plus
`thread_source ∈ {user,subagent,automation}`.

Identify rate-limit windows by `window_minutes`, not by whether they appear in `primary` or
`secondary` — a live file inspected during this check had the weekly window (`10080`) in `primary`
with `secondary: null`.

Codex additionally supports OTLP export via an `[otel]` block in `config.toml` (disabled by
default), which is the same shape as Claude Code's OpenTelemetry surface.

**Existing ecosystem:** `ccusage`, `codex-hud`, `tokscale`, `CodexBar` all parse the rollout JSONL successfully.

**Verdict:** Straightforward integration. Implemented as `codex_status.py`, which parses rollout JSONL directly, caches per-file summaries in `~/.codex/statusline_rollout_cache.json`, and renders a companion one-line monitor for prompt/tmux/sidecar usage.

## Pi coding harness — Easy

**Architecture:** TypeScript terminal harness with normalized, append-only session JSONL under `~/.pi/agent/sessions/`.

**Token data access:** Every persisted assistant message includes `usage.input`, `usage.output`, `usage.cacheRead`, `usage.cacheWrite`, `usage.reasoning`, and `usage.totalTokens`, plus provider and model identifiers. Reasoning is a subset of output.

**Implementation:** `pi_status.py` aggregates calls by response date, deduplicates entries copied by Pi's fork/clone session operations, and persistently caches parsed responses by file metadata. `pi_stepcount.py` provides shareable day/week/month summaries. Pi's native footer already handles live-session usage, so the monitor is a companion for cross-session totals rather than a replacement footer.

**Limitations:** Ephemeral `--no-session` runs leave no JSONL and cannot be counted afterward. Pi's internal compaction and branch-summary model requests are persisted without usage fields, so they are also omitted. Since Pi supports many providers, the shared energy constants are a provider-agnostic proxy rather than a basis for provider comparisons.

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

| Aspect | Claude Code | Codex CLI | Pi harness | Gemini CLI |
|--------|------------|-----------|------------|------------|
| Data access | Statusbar stdin JSON | JSONL rollout files | Session JSONL | OTel file (opt-in) or activity JSONL |
| Always available? | Yes | Unless `--ephemeral` | Unless `--no-session` | Activity logger: yes. OTel: opt-in |
| Cache read | Yes | Yes | Yes | Yes |
| Cache write | Yes | Yes (since 2026-08 check) | Yes | No |
| Reasoning tokens | Bundled in output | Separate subset | Separate subset | Separate field |
| Cross-session storage | Built-in | Session files | Session files | None |

## Architectural Implications

**What changes if we go multi-CLI:**

1. **Shared accumulation layer** — The daily JSON + history JSONL logic becomes shared code with per-CLI parsers feeding into it.

2. **File watching vs stdin** — Codex, Pi, and Gemini use file output, not stdin streams. They need file polling or watching for real-time monitoring; Claude Code's stdin statusline approach does not apply.

3. **Per-model energy constants** — Each CLI uses different model families with different energy profiles. Need a constants table keyed by model name/family, not just one set of constants.

4. **Unified token schema** — Different field names and semantics need normalization:
   - Cache write: Claude Code, Codex, and Pi expose it; Gemini does not
   - Reasoning tokens: Codex, Pi, and Gemini separate them; Claude Code doesn't
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
