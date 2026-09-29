# Changelog

All notable changes to this project are documented in this file. The project follows Semantic Versioning.

## [1.2.0] - Unreleased

### Added

- Installable `energy-monitor` CLI for durable local usage history and offline Tokenatlas reports.
- Incremental, retained SQLite history for Claude Code, Codex, Pi, and OpenCode observations.
- Cross-harness attribution by project, session, turn, model, effort, agent, and origin.
- Offline HTML reporting with filters, drilldown, exports, and qualified cache-read-share comparisons.
- Wheel installation, reinstall, uninstall, and private-data retention smoke coverage.
- CI coverage for Python 3.10 through 3.13 across Linux, macOS, and Windows, plus Chromium and WebKit.

### Changed

- `why.py` defaults to all supported harnesses while retaining `--harness both` for Claude and Codex.
- Session identities are harness-scoped in summaries and reports.
- OpenCode reasoning is normalized as a subset of output while retained as a separate subtotal.

### Security

- History persistence uses an explicit allowlist and never stores prompts, assistant text, tool content, credentials, hostnames, or hardware identifiers. It generates a random local ID to scope synthetic identities to one database.
- Shared reports pseudonymize project, session, turn, observation, and agent identities by default.
- Generated reports are standalone and make no network or model calls.

## [1.0.0] - 2026-05-30

- Initial Claude Code statusline release with token, cache, energy, and quota monitoring.
