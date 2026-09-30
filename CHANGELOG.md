# Changelog

All notable changes to this project are documented in this file. The project follows Semantic Versioning.

## [Unreleased]

### Added

- `tokenatlas refresh --all` (every harness from its default roots, missing ones `absent`, one JSON summary), a history `revision` counter with `report --html --if-changed --max-age 1h` (skips without touching the file when unchanged or too recent), and `tokenatlas open` (refresh, private report, browser); see README "Keeping the report fresh".
- List-price cost per observation (`tokenatlas/pricing.py`, packaged `tokenatlas/prices.json` with verified 2026-09-29 prices and source URLs): input, 5m/1h cache writes, cache read and output priced separately, long-context tiers, Claude fast mode and US inference; unknown prices or tariff dimensions give `n/a`/partial, never zero. Costs are API-equivalent at list price, not what was paid.
- `tokenatlas session <id>`: a session tree (Claude subagents and Workflow runs, explicit children, inferred headless children, unassigned threads) with per-model tokens and cost, conductor overhead, and cost per passed unit from an optional outcomes file; `tokenatlas rate` records outcomes.
- Claude desktop Cowork transcripts (`local-agent-mode-sessions/*/*/local_*/.claude/projects`) are imported by `refresh --harness claude` with origin `local-agent`; `audit.jsonl` is never read.
- `tokenatlas snapshot` and `import` merge history from other machines (see `docs/remote-machines.md`); `remote_sync.sh` pulls it.
- `tokenatlas overhead`: per-harness context floor, fixed-context component sizes (instruction files, skill listing, MCP instructions, system prompt), skill uses and an estimated recurring re-read cost. Only sizes, names and counts are stored.

### Changed

- Renamed to TokenAtlas: command `tokenatlas` (`energy-monitor` remains a deprecated alias), package `tokenatlas`, data directory `~/.local/state/tokenatlas` (migrated automatically from `agentmon`).
- Claude observations record their tariff (speed, service tier, inference geography); the collector version is 5, so existing files are re-read once.

## [1.2.0] - Unreleased

### Added

- Installable `energy-monitor` CLI for durable local usage history and offline Tokenatlas reports.
- Incremental, retained SQLite history for Claude Code, Codex, Pi, and OpenCode observations.
- Cross-harness attribution by project, session, turn, model, effort, agent, and origin.
- Offline HTML reporting with filters, drilldown, exports, and qualified cache-read-share comparisons.
- Wheel installation, reinstall, uninstall, and private-data retention smoke coverage.
- CI coverage for Python 3.10 through 3.13 across Linux, macOS, and Windows, plus Chromium and WebKit.

### Changed

- History database schema 2: typed columns and a string dictionary cut storage roughly sevenfold (990 MB to 140 MB for 379,401 real observations); existing databases migrate automatically on first open.
- `why.py` defaults to all supported harnesses while retaining `--harness both` for Claude and Codex.
- Session identities are harness-scoped in summaries and reports.
- OpenCode reasoning is normalized as a subset of output while retained as a separate subtotal.
- Claude subagent transcripts are linked to their parent session from the transcript path.
- Claude output is flagged as a lower bound (`output_not_final`) when a request's transcript lacks the final usage row; the collector version is bumped so existing files are re-read.
- Hour and minute buckets are ordered by instant across DST changes.
- Tokenatlas embeds a gzip-compressed columnar payload decoded offline in the browser: a 379,401-observation history renders to 10.5 MB (private) or 7.2 MB (shared) instead of 270 MB, and loads in about 2 s.
- Summary cards use plain terms with a one-line explanation (Tokens totalt, Cache-träffar, Anrop, Output-tokens) and Swedish number abbreviations (mdr, milj.).

### Security

- History persistence uses an explicit allowlist and never stores prompts, assistant text, tool content, credentials, hostnames, or hardware identifiers. It generates a random local ID to scope synthetic identities to one database.
- Shared reports show provider, origin, effort, and model names only from explicit public allowlists and pseudonymize everything else, including fine-tune ids and host names; a custom endpoint reusing a public provider id with a family-like model name is still shown, so review shared reports.
- Metadata strings, including iteration `model` and `type`, must be bounded printable text or they are dropped.
- Shared reports pseudonymize project, session, turn, observation, and agent identities by default.
- Generated reports are standalone and make no network or model calls.

## [1.0.0] - 2026-05-30

- Initial Claude Code statusline release with token, cache, energy, and quota monitoring.
