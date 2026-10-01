# Changelog

All notable changes to this project are documented in this file. The project follows Semantic Versioning.

## [Unreleased]

### Added

- Harness logs moved with the harness's own variable are found: `CLAUDE_CONFIG_DIR`, `CODEX_HOME` (sessions and `session_index.jsonl`), `PI_CODING_AGENT_DIR` and an absolute `XDG_DATA_HOME` (OpenCode); an explicit root still wins, and `doctor` reports each resolved root and its source (#52).

## [1.6.1] - 2026-10-01

### Fixed

- `tokenatlas collect`: the report after the remote sync also uses `--max-age 1h`, so the report is built at most once an hour while only the data changes, as with the old shell collector; it was rebuilt on every run (about 90 s CPU each on 400k observations) (#49).

## [1.6.0] - 2026-10-01

### Changed

- `tokenatlas top` shows and `top --keep-text` stores the top 10 turns by default (was 5), and the report's "Costliest turns" card shows 10. An existing store keeps its recorded k; move it to 10 once with `tokenatlas top --keep-text -n 10`.
- `tokenatlas collect` runs its `top --keep-text` step with the store's recorded `-n` and `--by` instead of the defaults, so a chosen k is no longer reset and entries are no longer evicted; it never raises k on its own. A store without a valid recorded k/by (corrupt, unsafe or hand-edited) makes `collect` skip the step, log why and exit 1 without touching the file; the report shows no stored text for such a store.
- Stored prompt text and context outlive harness log cleanup (for example Claude Code's `cleanupPeriodDays`) while the turn stays in the top k; `top --forget-text` deletes it.

## [1.5.1] - 2026-10-01

### Fixed

- Remote sync with macOS `/usr/bin/rsync` (openrsync): a missing remote file is again a benign "not found" instead of an error that made `collect` exit 1 on every run (#43). openrsync prints a `receiver has empty file list` warning and no `rsync error:` summary; that warning is accepted only together with the sender's missing-file line for the requested file.

## [1.5.0] - 2026-10-01

### Added

- `tokenatlas collect` replaces the reference shell collector `scripts/collect.sh`: one scheduled command that refreshes, stores top-turn text if opted in, builds the conditional private report, runs the remote sync and rebuilds the report after every attempted sync. It takes a kernel lock (`flock`, `msvcrt.locking` on Windows) on `collect.lock`, so runs never overlap and a crashed run leaves nothing stale; a busy lock prints `collect: already running` and exits 0. `--remote tag:host`, `--remote-sync`, `--sync-timeout`, `--no-report`, `--lang`. The remote sync script now ships in the package as `tokenatlas/remote_sync.sh`; the repository-root `remote_sync.sh` is a shim.
- Cost facts: `tokenatlas insights [--days N | --start/--end] [--json]` prints deterministic, rule-based list-price facts (cost by model, a neutral price ladder of the same tokens at every model of the same provider (the model used is marked; no recommendation), cost by token class, input size per request, long-context premium, big turns, subagent share, fast/priority tier extra) with the computation and assumptions of each; no model, no interpretation, aggregate only. The report gets a "Kostnadsfakta" / "Cost facts" card for the last 30 days and all history, computed when the report is built, with measured/computed badges; shared reports apply the usual model-name redaction.
- Cost facts follow the report's reliability rules: ambiguous-identity observations are left out of every fact, incomplete ones make amounts lower bounds (`≥`) (the long-context premium and premium-tier extra cost, which are differences between price tiers, use complete requests only and state how many were left out), both counts and the pricing assumptions (with request counts) are stated per fact, the 30-day window ends at one captured `now`, and the price table used is named with its `retrieved_on` date.
- README: what TokenAtlas adds beyond the vendors' own tools, and what to use the vendor tools for.

### Changed

- `report_state` takes the UTC day of the rolling 30-day window, so a report is rebuilt once per day even when the history is unchanged.

### Fixed

- Remote sync can no longer hang on a stalled host (#39): `remote_sync.sh` bounds every ssh/scp/rsync call (connect timeout, keep-alive, `BatchMode`, rsync `--timeout`, `TOKENATLAS_SSH_OPTS`) and each host as a whole (`TOKENATLAS_HOST_TIMEOUT`, default 300 s; reported as `ERROR (timeout after Ns)`); `tokenatlas collect --sync-timeout` (default 600 s) bounds the whole sync and kills its process group. The script closes the signal window before its worker pid is recorded, and rsync exit 23 is a benign missing file only when the error names the remote path ("No such file or directory" for a local destination is now a failure).

## [1.4.0] - 2026-10-01

### Added

- Turn context: `top --keep-text` also stores, for the current top turns only and in the same 0600 `top-prompts.json` (now version 2; version 1 is still read and upgraded), the title (Claude `custom-title`, Codex `session_index` thread name, OpenCode session title, Pi `session_info` name), working directory, branch, repository, input count, initiating and follow-up inputs, final message, shell/edit/web/subagent counts, PR numbers and up to 5 commit subjects from local `git log --all` over the turn window. No network, no model calls. `top` prints up to four context lines per turn and `--json --with-text` adds `context`. Private reports get an "Inputs" column and an expandable context block per stored turn; shared reports carry only the input count and never any context text. See README "Top turns".
- Prompts are now called turns: `top` ranks the costliest turns (an initiating input plus everything it caused, including follow-up inputs and subagent work) and the report card is "Dyraste turerna" / "Costliest turns". The command name and JSON keys are unchanged.
- English report UI: `report --html` and `open` take `--lang auto|sv|en` (default `auto`: Swedish when the browser language starts with `sv`, otherwise English), and the report header has an SV/EN toggle that switches live and is remembered in `localStorage` (when available). Pseudonym labels in the payload are now language-neutral codes; the UI texts ship as a second compressed block (`report_i18n.json`).
- Release workflow `.github/workflows/publish.yml` publishes to PyPI with trusted publishing (no stored token) when a GitHub release is published, or on demand for an existing tag.

## [1.3.0] - 2026-09-30

### Added

- Top prompts: `tokenatlas top` ranks the costliest prompts (subagent work rolled up); opt-in `--keep-text` / `--forget-text` / `--with-text` manage `top-prompts.json` (0600, top prompts only, never in the database or snapshots); the report gets a "Dyraste prompterna" card that follows the filters, priced client-side from the token columns and a small list of unit-price classes (the report grows about 5%), with prompt previews in private reports only. See README "Top prompts".
- Pi observations get derived turn ids (Pi files are re-read once).

### Fixed

- Codex usage is attributed to the explicit turn ids of current rollouts; assistant and developer messages no longer start a new turn, which had split one prompt into many fragments. Stored turn ids are corrected on the next refresh, which re-reads Codex files once (about 2–3 minutes for 10k rollouts).

## [1.2.0] - 2026-09-30

### Added

- `tokenatlas refresh --all` (every harness from its default roots, missing ones `absent`, one JSON summary), a history `revision` counter and a report state marker (`tokenatlas-state`: option identity plus data revision and coverage; options are never throttled by `--max-age`) with `report --html --if-changed --max-age 1h` (skips without touching the file when unchanged or too recent), and `tokenatlas open` (refresh, private report, browser; reuses an unchanged report); see README "Keeping the report fresh".
- List-price cost per observation (`tokenatlas/pricing.py`, packaged `tokenatlas/prices.json` with verified 2026-09-29 prices and source URLs): input, 5m/1h cache writes, cache read and output priced separately, long-context tiers, Claude fast mode and US inference; unknown prices or tariff dimensions give `n/a`/partial, never zero. Costs are API-equivalent at list price, not what was paid.
- `tokenatlas session <id>`: a session tree (Claude subagents and Workflow runs, explicit children, inferred headless children, unassigned threads) with per-model tokens and cost, conductor overhead, and cost per passed unit from an optional outcomes file; `tokenatlas rate` records outcomes.
- Claude desktop Cowork transcripts (`local-agent-mode-sessions/*/*/local_*/.claude/projects`) are imported by `refresh --harness claude` with origin `local-agent`; `audit.jsonl` is never read.
- `tokenatlas snapshot` and `import` merge history from other machines (see `docs/remote-machines.md`); `remote_sync.sh` pulls it.
- `tokenatlas overhead`: per-harness context floor, fixed-context component sizes (instruction files, skill listing, MCP instructions, system prompt), skill uses and an estimated recurring re-read cost. Only sizes, names and counts are stored.
- Installable `energy-monitor` CLI for durable local usage history and offline Tokenatlas reports.
- Incremental, retained SQLite history for Claude Code, Codex, Pi, and OpenCode observations.
- Cross-harness attribution by project, session, turn, model, effort, agent, and origin.
- Offline HTML reporting with filters, drilldown, exports, and qualified cache-read-share comparisons.
- Wheel installation, reinstall, uninstall, and private-data retention smoke coverage.
- CI coverage for Python 3.10 through 3.13 across Linux, macOS, and Windows, plus Chromium and WebKit.

### Changed

- Renamed to TokenAtlas: command `tokenatlas` (`energy-monitor` remains a deprecated alias), package `tokenatlas`, data directory `~/.local/state/tokenatlas` (migrated automatically from `agentmon`).
- Claude observations record their tariff (speed, service tier, inference geography); the collector version is 5, so existing files are re-read once.
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
