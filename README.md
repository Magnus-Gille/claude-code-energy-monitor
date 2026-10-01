# TokenAtlas

*Formerly claude-code-energy-monitor.*

TokenAtlas is a local, private token and cost atlas for AI coding agents (Claude Code, Codex, OpenCode, Pi): a durable history CLI (`tokenatlas`), an offline HTML report, list-price costing, session trees and fixed-context overhead. The statusline and companion scripts below are the original energy monitor and keep working from a checkout.

### What TokenAtlas adds beyond the vendors' own tools

- One local, private view across Claude Code, Codex, OpenCode and Pi, and across machines (snapshots merge into one history). Nothing leaves the machine and no model is called.
- The costliest turns, each with its context: an initiating input plus everything it caused, including follow-up inputs and subagent work.
- A list-price dollar valuation of usage, including subscription usage, so that very different plans and harnesses can be compared in one unit (API-equivalent, not what was paid).
- Long history: observations are kept in a local database after the harnesses rotate or delete their logs.

Use the vendor tools for:

- Plan limits and resets (Claude Code `/usage`, Codex `/usage`).
- Actual billing and invoices.
- Organization admin dashboards.

A statusline script for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that shows real-time token usage and order-of-magnitude energy estimates. It tracks daily, weekly, and monthly totals, distinguishes cheap cached tokens from expensive fresh tokens, and logs history automatically.

The repo also includes companion monitors for Codex CLI (`codex_status.py`) and the Pi coding harness (`pi_status.py`). Both read their harness's session JSONL and print the same style of one-line summary for a prompt, tmux status, or sidecar terminal.

```
Opus 4.8 | Ctx:42% | 5h:29% 7d:52% | D:2.0M ~2kWh | W:45.3M ~20kWh | M:412M ~50kWh
```

Reading left to right:

| Segment | Meaning |
|---------|---------|
| `Opus 4.8` | Active model |
| `Ctx:42%` | Context-window utilization (current session) |
| `5h:29% 7d:52%` | API quota consumption (5-hour and 7-day rolling windows) |
| `D:2.0M ~2kWh` | Daily total tokens and energy estimate |
| `W:45.3M ~20kWh` | Weekly total (rolling 7 days) |
| `M:412M ~50kWh` | Monthly total (rolling 30 days) |

Energy is **model-weighted** (Haiku ×0.3, Sonnet ×0.6, Opus ×1.0; order-of-magnitude) for today and going forward; older history days are model-agnostic. Quota prefers the statusline payload's `rate_limits` (no API call), falling back to the OAuth usage endpoint when absent.

## Explain a spike across Claude, Codex, Pi, and OpenCode

`why.py` reads retained Claude Code transcripts, Codex rollouts, Pi sessions, and OpenCode's local
usage store directly and ranks the calls that consumed a time window. It groups by project, main
thread vs subagent, entrypoint, agent, exact model, effort, and session. Codex Desktop calls are
included when they are present in the shared `~/.codex/sessions/` directory.

```bash
# All supported harnesses, trailing 24 hours
python3 why.py

# A short burst, or one local calendar day
python3 why.py --hours 0.5
python3 why.py --date 2026-09-03

# Narrow to one harness or emit structured output
python3 why.py --harness codex --limit 10
python3 why.py --hours 6 --json
```

The command is stateless: it writes no ledger or cache. Claude requests are deduplicated by
`requestId` using the maximum of each streamed token field; Codex uses each turn's
`last_token_usage` delta and discards repeated cumulative snapshots. Pi deduplicates copied fork
history by provider response ID and preserves the original session attribution. OpenCode transiently
parses local message records, then retains only allowlisted usage and attribution fields. Resumed sessions land on the
day when each call actually happened. For a full Claude calendar day the command also compares
transcript totals with the older statusline counter and reports the latter's coverage. Use
`--harness both` for the legacy Claude+Codex pair, or select `claude`, `codex`, `pi`, or `opencode`.

## Durable local usage history

`tokenatlas` imports Claude, Codex, Pi, and OpenCode observations into a local SQLite
history. It needs Python 3.10+ and the standard library, makes no network or LLM
calls, and does not change your installed statusline.

```bash
# Default store: ~/.local/state/tokenatlas/history.sqlite3 (or XDG_STATE_HOME)
tokenatlas refresh --harness claude
tokenatlas refresh --harness codex
tokenatlas refresh --harness pi
tokenatlas refresh --harness opencode
tokenatlas refresh --all   # every harness from its default roots (Claude incl. Cowork)
tokenatlas doctor

# Stored history, local time buckets; timestamps must include an offset
tokenatlas report --start 2026-09-01T00:00:00+02:00 --end 2026-10-01T00:00:00+02:00 --granularity hour
tokenatlas report --granularity minute --session SESSION_ID --records

# Isolated input/state, useful for a demonstration or fixture
tokenatlas --db /private/tmp/tokenatlas-demo/history.sqlite3 refresh --harness claude --root /path/to/transcripts
```

All commands print JSON. `refresh` imports only changed files; running it twice
or importing a copied transcript does not add the same usage again. Recorded
history survives source deletion. Schedule refresh separately if desired;
there is no background service, and already-deleted logs cannot be recovered.
`refresh` exits 2 for missing sources or partial imports and records diagnostics.

Logs are read from each harness's default location unless the harness itself has been moved with its own
variable, which TokenAtlas reads at every run (an unset or empty variable means the default):

| Harness | Variable | Read from | Default |
| --- | --- | --- | --- |
| Claude Code | `CLAUDE_CONFIG_DIR` | `$CLAUDE_CONFIG_DIR/projects` | `~/.claude/projects` |
| Codex | `CODEX_HOME` | `$CODEX_HOME/sessions` and `session_index.jsonl` | `~/.codex/sessions` |
| Pi | `PI_CODING_AGENT_DIR` (leading `~` expanded) | `$PI_CODING_AGENT_DIR/sessions` | `~/.pi/agent/sessions` |
| OpenCode | `XDG_DATA_HOME` (must be absolute, else ignored) | `$XDG_DATA_HOME/opencode/opencode.db` | `~/.local/share/opencode/opencode.db` |

An explicit command-line root (`--root`, `--claude-root`, ...) beats the variable, which beats the default.
The Claude desktop Cowork location is not affected by `CLAUDE_CONFIG_DIR`. `tokenatlas doctor` lists the
resolved path and its source (`default` or the variable name) per harness under `roots`. A scheduled job
(cron, launchd) does not inherit your interactive shell's environment: set the variable in the schedule
itself, for example `CODEX_HOME=/data/codex tokenatlas refresh --all` in the crontab line, or under
`EnvironmentVariables` in the launchd plist.

`report` supports day/hour/minute, `--timezone`, `--harness`, `--project` (full
identity), `--session` (including displayed `HARNESS:SESSION_ID` names), and `--turn`. `--records` includes source-file references
and observed/derived turn links. The output is **private local data**: project
paths are visible, although prompt content and tool inputs are never stored.

Missing counters remain null; `known_tokens` is only the identified observed subtotal.
Ambiguous identities are excluded and reported separately because they may overlap.
A complete normalized observation does not mean complete account coverage or
verified billing. Nontrivial Claude `iterations` are retained and flagged rather
than silently ignored or added twice. Claude subagent transcripts often lack the final
usage row of a request, so their output can be a lower bound (warning `output_not_final`). See [accounting and storage decisions](docs/history-accounting.md).

## Keeping the report fresh

A report is a snapshot. To keep one current without manual steps, refresh on a schedule and rebuild the
report only when the data changed and the file is old enough. The history keeps a `revision` counter
(shown by `doctor`) that grows whenever a refresh or import stores something new. The report records a
state fingerprint in a `<meta name="tokenatlas-state">` tag; it covers the data revision, the report options
(privacy, timezone, granularity, filters) and the coverage, so changing any of them rebuilds the report.

```bash
# Every harness from its default roots; a harness that is not installed here is reported as "absent"
tokenatlas refresh --all
# Rebuild only if data changed (--if-changed) and the report is older than 1h (--max-age: 90s, 30m, 1h, 2d)
tokenatlas report --html ~/.local/state/tokenatlas/report.html --private --if-changed --max-age 1h
```

`refresh --all` prints one JSON document with a `status` (`ok`, `partial` or `missing`, the worst of the
present harnesses) and one entry per harness. It exits 0 when every present harness is `ok` and 2
otherwise; `absent` harnesses never fail it, so a machine without Claude Code still exits 0. A skipped
report prints `{"html": ..., "skipped": true, "reason": "unchanged"|"too recent"}`, exits 0 and does not
touch the file. Without `--if-changed` and `--max-age`, `report --html` always rebuilds. A refresh that
finds nothing new leaves the revision alone; a re-import of identical rows with known sources does too.
`tokenatlas open` reuses a background report built with
`report --html $XDG_STATE_HOME/tokenatlas/report.html --private` when nothing changed, and only opens it.

The report is available in Swedish and English. `--lang auto|sv|en` (on `report` and `open`, default `auto`) picks the language: `auto` follows the browser (`sv` gives Swedish, anything else English). The SV/EN toggle in the report header switches live and remembers the choice in the browser when storage is available; an explicit `--lang` wins over a remembered choice. `--lang` is part of the report identity, so changing it rebuilds a conditional report.

`tokenatlas collect` does all of this in one scheduled command. Nothing to copy: the remote sync script
ships inside the package (`tokenatlas/remote_sync.sh`; override with `--remote-sync PATH` or
`TOKENATLAS_REMOTE_SYNC`). The steps run in this order, so local results never wait for a remote machine:

1. refresh all harnesses (`refresh --all`)
2. `top --keep-text`, only if you opted in (`top-prompts.json` exists in the state directory); it keeps the `-n` and `--by` the store was created with
3. the conditional private report to `report.html` in the state directory (`--if-changed --max-age 1h`)
4. then, only if hosts are configured (`--remote tag:host`, repeatable; else `REMOTE_HOSTS_OVERRIDE`; else a
   `remote-hosts` file of space-separated `tag:host` pairs in the state directory), the remote sync with
   bounded ssh/scp/rsync calls (see [Other machines](docs/remote-machines.md)). Skipped on Windows.
5. the conditional report again after every attempted sync, successful or not, with the same `--if-changed --max-age 1h`:
   imported data shows up at the next build, at most an hour later, so the report is built at most once an hour while
   only the data changes (`tokenatlas open` gives an exact view on demand)

`--no-report` skips both reports and `--lang auto|sv|en` is passed to them. The exit code is 0 when every step
succeeded and 1 when any failed (a failed sync still gets its second report).

One run at a time, by a kernel lock: `collect` takes an exclusive `flock` on `collect.lock` in the state
directory (`msvcrt.locking` on Windows). A second run prints `collect: already running` and exits 0. The kernel
releases the lock when the process exits or crashes, so there is no stale-lock cleanup; the file itself is kept.

Time limits: every ssh/scp/rsync call is bounded, each host gets `TOKENATLAS_HOST_TIMEOUT` seconds (default 300), and
the whole sync gets `--sync-timeout` seconds (default 600). Past that, `collect` sends SIGTERM to the sync's whole
process group, waits 2 seconds, then SIGKILL, and logs `remote sync: timeout after Ns`. The log is one line per
step with its exit status and duration.

Cron, every 30 minutes (use the full path; cron has a short `PATH`):

    */30 * * * * $HOME/.local/bin/tokenatlas collect --remote pi:myhost >> ~/Library/Logs/tokenatlas/collect.log 2>&1

(`mkdir -p ~/Library/Logs/tokenatlas` first; on Linux use e.g. `~/.local/state/tokenatlas/collect.log`.) Without
remote machines, drop `--remote`. On macOS prefer launchd: it runs a missed job after sleep, cron does not. Save as
`~/Library/LaunchAgents/com.tokenatlas.report.plist` (replace `YOU` with your user name; launchd does not expand
`$HOME`) and run `launchctl load` on it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tokenatlas.report</string>
  <key>StartInterval</key><integer>1800</integer>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/.local/bin/tokenatlas</string><string>collect</string>
    <string>--remote</string><string>pi:myhost</string>
  </array>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/tokenatlas/collect.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/tokenatlas/collect.log</string>
</dict></plist>
```

On a laptop that sleeps, the background report can be hours old after a wake. Run `tokenatlas open` when
you want an exact view: it runs `refresh --all` (continuing if a harness is partial), writes a **private**
report to `$XDG_STATE_HOME/tokenatlas/report.html` (or `--html PATH`; `--shared` pseudonymizes it) and
opens it in your default browser. `--no-refresh` skips the refresh. `report` itself keeps the shared,
pseudonymized default unless you pass `--private`.

## Session trees and outcomes

```bash
# Tree of one root session: subagents, workflows, child sessions, per-model totals
tokenatlas session <session-id> [--json] [--no-infer]

# List thread keys, then rate a unit of work (appends to outcomes.jsonl next to the database)
tokenatlas rate <session-id>
tokenatlas rate <session-id> --unit review --thread claude:agent:<id> --outcome pass --note "ok"
```

Costs are API-equivalent list prices, not what was paid; unknown cost prints `n/a`. Once outcomes exist,
`session` also shows cost per passed result per model. Details and limits: [docs/sessions.md](docs/sessions.md).

## Top turns

```bash
tokenatlas top [-n 10] [--by cost|tokens] [--harness H] [--project P] [--start ISO] [--end ISO] [--json]
tokenatlas top --keep-text          # opt in: remember the text and context of the current top 10 turns (-n N for another k)
tokenatlas top --forget-text        # delete the stored text and context
tokenatlas top --json --with-text   # include stored text and context in JSON
```

A turn is your initiating input plus everything it caused, including follow-up inputs and subagent work (rolled up by
time and parent session). Turns are ranked by list-price cost (unpriced last, and any non-USD price counts as unpriced) or by tokens; cost is API-equivalent,
`n/a` when unpriced and `≥` when only partly priced. The HTML report has a matching "Dyraste turerna" / "Costliest turns" card
that follows the report filters and has an "Inputs" column (the number of inputs in the turn, `–` when unknown).

Prompt text is never stored in the history database and never in snapshots or imports. Only if you run
`top --keep-text` does TokenAtlas write `top-prompts.json` next to the history (mode 0600, written atomically).
It holds, for the current global top turns only, the harness, session, turn id, capture time, a sanitized
preview (whitespace collapsed, secret-like strings masked, at most about 200 characters) and the turn context below,
all read from this machine's own logs; turns from other machines get neither. A turn that falls out of the top is removed
from the file on the next `--keep-text`. Turns whose text or context could not be read are retried on each `--keep-text`,
which also fills the context of entries kept by an older version (the file format is version 2; version 1 files are still read).
The default is the top 10 turns. A store created with a smaller k stays at that k, also under `collect`, which reuses the store's recorded `-n` and `--by`
and never raises k on its own. A store without a valid recorded `-n`/`--by` (corrupt, unsafe or hand-edited) makes `collect` skip the text step, log why and exit 1, leaving the file untouched; fix it with `tokenatlas top --keep-text -n N` or `--forget-text`; move an existing store to 10 once with `tokenatlas top --keep-text -n 10`. Stored text outlives the harness's own
log cleanup (for example Claude Code's `cleanupPeriodDays`) while the turn stays in the top k; `top --forget-text` deletes it.

### Turn context

With `--keep-text`, each stored turn also gets best-effort, local-only context, sanitized the same way:

- **title**: Claude `custom-title`, Codex `session_index.jsonl` thread name, OpenCode session title, Pi `session_info` name;
- **place**: working directory, git branch and repository (credentials, query and fragment stripped) as recorded by the harness;
- **inputs**: the number of inputs, the initiating input and up to 5 short follow-up inputs;
- **final message**: the last assistant text of the turn, at most about 400 characters;
- **activity**: counts of shell, edit, web and subagent tool calls (command text is scanned for PRs, never stored);
- **outcomes**: PR numbers seen in `gh pr` commands or the final message, and up to 5 commit subjects from local `git log --all`
  over the turn's time window in the turn's working directory.

Nothing is fetched from the network and no model is called. `top` prints up to four indented lines per stored turn
(title or initiating input; `repo/branch · directory`; counts and outcomes; `final: …`); `--json --with-text` adds a `context` object.
Private reports (`report --html --private`, `open` without `--shared`) show the stored previews and an expandable context block for
the turns in the current global top (by the recorded `-n` and ranking) when the file exists. Shared (pseudonymized, default)
reports never contain any text, title, path, branch, repository, final message, PR number or commit subject; the only
context they carry is the number of inputs per turn.

On POSIX the store is only read when it is a regular file owned by you with mode 0600 and no other hard links; a symlink,
a foreign owner, group/other access or extra links are refused with a warning, and `--keep-text` replaces such a file
with a fresh 0600 one (never following a symlink). `--forget-text` unlinks the file itself and warns if other hard links still
hold the text. On Windows these owner and mode checks are unavailable: the file relies on the user profile's ACLs.

## Cost facts

`tokenatlas insights [--days N | --start ISO --end ISO] [--json] [--prices FILE]` prints deterministic, rule-based facts about list-price cost. There is no language model and no interpretation: each fact is a number computed from the saved observations and the selected price table (the packaged one unless `--prices` is given; its `retrieved_on` date is shown), with the computation and the assumptions printed next to it. A fact that has no data, or cannot be computed reliably, is omitted. Without a window the whole history is used; `--json` prints the same facts as data. The output is aggregate only: no project names, sessions, paths or prompt text. The report has the same facts in a "Kostnadsfakta" / "Cost facts" card for the last 30 days and for all history; they are computed when the report is built (the report is rebuilt when the UTC day changes) and do not follow the page filters. In a shared report model names go through the same redaction as the rest of the report.

Each fact is marked **measured** (counted from the logs) or **computed** (arithmetic on measured values with the price table). Reliability follows the report's own totals: an observation with an ambiguous (synthetic) identity is left out of every fact, and an incomplete one (lower-bound token counters) is kept but marks the affected amounts as lower bounds (`≥`), except in the two facts that are a difference between price tiers (`long_context_premium`, `premium_tiers`): there a difference over unknown tokens has no safe bound, so they use only requests with complete counters, are exact for those, and state how many were left out; every fact states both counts, and the pricing assumptions it relies on (for example "speed not recorded, priced as standard") with the number of requests each applies to. "Last 30 days" ends at the moment the report is built (or the `--days` command runs); later-dated observations are not in it. Per-model counts are priced requests only. Cost is the API-equivalent list price in USD: only requests with a complete USD price count; the others (unknown model, missing price, non-USD price, local models) are counted as unpriced and left out of every cost.

| Fact | Exact definition |
|------|------------------|
| `model_share` (computed) | Per model (aliases in the price table count as one): sum of the list-price cost of its priced requests in the window / sum over all priced requests. The five largest are listed, the rest summed as other. Also `unpriced_requests` and `unpriced_share` = requests without a complete USD price / all requests. |
| `price_comparison` (computed) | A neutral price ladder, not a recommendation. For each model with at least 10% of priced cost: the exact same requests (same token counts and cache classes) priced again at every model of the same provider in the price table that has complete USD prices and can price all of them, the model used included and marked (free models and non-USD prices are not listed). The long-context tier is evaluated per request with each model's own threshold and rates; the tier, cache and modifier rules are the ones used for the original price. Models are listed by cost, highest first, at most 8; when there are more, the 8 closest to the model used in that order are shown. Assumption: same token counts; output quality and token counts of another model are not measured. |
| `cost_parts` (computed) | Priced cost split into input (uncached), cache write, cache read and output, summed over priced requests; share = part / total priced cost. Reasoning tokens are part of output. |
| `context_size` (measured) | Per harness: input tokens per request = fresh input + cache read + cache write, over requests where all three are known. Median = middle value (mean of the two middle values for an even count); p90 = nearest rank, the value at position ceil(0.9 x n) in ascending order. |
| `long_context_premium` (computed) | Requests whose total input exceeds the model's long-context threshold are priced at the long-context rates. Premium = their cost at those rates - their cost at the standard rates, same tokens. Only requests with complete token counters (an incomplete request of a model with a long-context tier is left out and counted, since even its tier is uncertain). Omitted when no such request is at a long-context tier. |
| `big_turns` (computed) | Turns (as in `top`, subagent work rolled up) whose cost from priced requests in the window is >= $50: count, share = their cost / cost of all turns with at least one priced request in the window, and the median number of requests per such turn. Requests that cannot be tied to a turn are excluded, a turn counts only the requests inside the window, and unpriced requests add nothing, so a turn's cost is a lower bound. Omitted when no turn reaches the threshold. |
| `subagent_share` (computed) | List-price cost of requests from subagent threads / list-price cost of all priced requests. Omitted when no priced request is from a subagent. |
| `premium_tiers` (computed) | For requests priced with a fast (Claude speed or service tier) or priority modifier: extra = their cost at the tier's prices - their cost at the standard prices, same tokens. Flex (discounted) tiers are not included. Only requests with complete token counters (the others are left out and counted). Omitted when no such request uses such a tier. |

## Fixed context overhead

`tokenatlas overhead --refresh` reports the floor tokens of a session's first request, the sizes of
instruction files, skills lists and skill bodies, and an API-equivalent estimate of re-reading that floor on
every call. Only sizes, names, counts and token counts are stored, never content. Component tokens are
character counts divided by 4 (an estimate). A comparison table at the top gives usage-normalized measures per
harness (fixed share of input, per 1k output, share of cost, calls per session), sorted most efficient first. See [docs/overhead.md](docs/overhead.md).

## Install the history and TokenAtlas CLI

Python 3.10 or newer is required. `pipx` keeps the command isolated from other Python tools:

```bash
pipx install tokenatlas
tokenatlas --version
```

TokenAtlas is published on [PyPI](https://pypi.org/project/tokenatlas/). For the latest unreleased code use
`pipx install git+https://github.com/Magnus-Gille/tokenatlas`, and for a checkout `pipx install /path/to/tokenatlas`. On systems with an externally managed Python (for example Raspberry Pi OS), use the venv recipe in [docs/remote-machines.md](docs/remote-machines.md). The old `energy-monitor` command remains as a deprecated alias that prints a one-line notice.

Upgrade or remove the command with:

```bash
pipx upgrade tokenatlas
pipx uninstall tokenatlas
```

Uninstalling the command intentionally preserves the local history database at
`~/.local/state/tokenatlas/history.sqlite3` (or `$XDG_STATE_HOME/tokenatlas/history.sqlite3`). Back it up
or remove it separately according to your own data-retention policy. Running `python3 -m tokenatlas` from
a source checkout remains supported. On first run the former `agentmon` data directory is moved to `tokenatlas` automatically (never with an explicit `--db`).

## Install the Claude Code statusline

**30-second setup** — paste this into Claude Code:

> Please set up a custom statusline for me. Do the following:
>
> 1. Download `statusline.py` from https://github.com/Magnus-Gille/tokenatlas and save it to `~/.claude/statusline.py`
> 2. Run `chmod +x ~/.claude/statusline.py`
> 3. Run `claude config set --global statusline "python3 ~/.claude/statusline.py"` to enable it

Or do it manually:

```bash
# Download the script
curl -o ~/.claude/statusline.py https://raw.githubusercontent.com/Magnus-Gille/tokenatlas/master/statusline.py

# Make it executable
chmod +x ~/.claude/statusline.py

# Register it with Claude Code
claude config set --global statusline "python3 ~/.claude/statusline.py"
```

That's it. The statusline appears the next time you start a Claude Code session.

## Codex CLI

Codex support lives in [`codex_status.py`](codex_status.py). It is not injected into the Codex TUI itself; Codex currently writes rollout logs instead of calling an external statusline command.

```bash
# From this repo
chmod +x ./codex_status.py

# Print one status line for the latest Codex rollout
python3 ./codex_status.py

# Continuously refresh it in a side terminal
python3 ./codex_status.py --watch
```

Example output:

```
gpt-5.4 | Win:258k | 5h:36% 7d:46% | D:1.8M ~100Wh | W:27.4M ~5kWh | M:75.6M ~10kWh
```

Useful integrations:

```bash
# zsh right prompt
setopt PROMPT_SUBST
RPROMPT='$(python3 /path/to/codex_status.py 2>/dev/null)'

# tmux status-right
set -g status-right "#(python3 /path/to/codex_status.py 2>/dev/null)"
```

Notes:
- Codex rollout logs expose total input, cached input, output, reasoning output, context window, and 5h/7d rate-limit usage.
- Current Codex rollouts expose cache-write tokens. `why.py` reports them; the older energy status scripts do not yet apply a distinct cache-write energy constant.
- Parsed rollout summaries are cached in `~/.codex/statusline_rollout_cache.json` to keep prompt-time execution reasonably fast.

### Codex step counter

For copy/pasteable summaries similar to `stepcount.py`, use [`codex_stepcount.py`](codex_stepcount.py):

```bash
python3 ./codex_stepcount.py
python3 ./codex_stepcount.py -d
python3 ./codex_stepcount.py -w
python3 ./codex_stepcount.py -m
python3 ./codex_stepcount.py -t
python3 ./codex_stepcount.py --rough-energy-estimate
python3 ./codex_stepcount.py --copy
```

Example output:

```text
⚡ Codex
   Today  5.1M tokens ·    5 sessions
   Week  30.8M tokens · 1395 sessions
   Month 79.0M tokens · 2129 sessions
```

### Print after Codex exits

Codex still has no native Stop hook, so the practical equivalent is a wrapper that runs Codex and then prints the summary after the process exits.

This repo includes [`codex_with_summary.py`](codex_with_summary.py):

```bash
python3 ./codex_with_summary.py
python3 ./codex_with_summary.py resume --last
python3 ./codex_with_summary.py exec "fix the failing tests"
```

If you want this behavior on your normal `codex` command, add a shell function to your `~/.zshrc`:

```bash
codex() {
  python3 /path/to/codex_with_summary.py "$@"
}
```

Optional:
- Set `CODEX_SUMMARY_ARGS="--rough-energy-estimate"` to change the summary flags.
- The wrapper intentionally skips printing a summary for `--help`, `--version`, `completion`, `features`, `login`, `logout`, `mcp`, and `debug`.

## Pi coding harness

Pi support lives in [`pi_status.py`](pi_status.py). Pi already displays live-session token and cache usage in its footer; this companion adds daily, weekly, and monthly totals with the repository's energy proxy by reading `~/.pi/agent/sessions/`.

```bash
# Print one status line from saved Pi sessions
python3 ./pi_status.py

# Continuously refresh it in a side terminal
python3 ./pi_status.py --watch

# Emit machine-readable totals
python3 ./pi_status.py --json
```

Example output:

```text
gpt-5.6-sol | D:512k ~200Wh | W:1.8M ~500Wh | M:4.2M ~1kWh
```

For shareable summaries:

```bash
python3 ./pi_stepcount.py
python3 ./pi_stepcount.py -d
python3 ./pi_stepcount.py -w
python3 ./pi_stepcount.py -m
python3 ./pi_stepcount.py -t
python3 ./pi_stepcount.py --rough-energy-estimate
python3 ./pi_stepcount.py --copy
```

Notes:
- Pi records normalized `input`, `output`, `cacheRead`, `cacheWrite`, and `reasoning` usage on every assistant response. Reasoning is a subset of output and is not added twice.
- Calls are assigned to the date of each assistant response, including sessions continued on later days.
- Pi forks and clones can copy earlier entries into a new session file. The monitor deduplicates copied responses while retaining genuinely new calls on each branch.
- Pi's internal compaction and branch-summary model requests are persisted without usage fields, so their tokens and energy cannot be recovered from session JSONL and are omitted.
- `--no-session` runs are not persisted and therefore cannot be counted afterward.
- Parsed responses are cached by file size and modification time in `~/.pi/agent/statusline_session_cache.json` for prompt, tmux, and watch performance.
- Pi can use many providers. The energy values remain the same documented, provider-agnostic order-of-magnitude proxy; they are not suitable for comparing providers or models.
- [`pi_scanner.py`](pi_scanner.py) is unrelated: its name refers to a Raspberry Pi machine running headless Claude Code jobs.

Useful integrations are the same as for Codex:

```bash
# zsh right prompt
setopt PROMPT_SUBST
RPROMPT='$(python3 /path/to/pi_status.py 2>/dev/null)'

# tmux status-right
set -g status-right "#(python3 /path/to/pi_status.py 2>/dev/null)"
```

## How Claude Code monitoring works

1. **Claude Code calls the script** on every status update (after each API call, during streaming). It pipes a JSON object with session data into stdin.
2. **The script reads the data**, computes energy estimates, updates daily totals, and prints one line to stdout — which Claude Code renders in the status bar.
3. **Daily totals persist** across sessions in `~/.claude/statusline_daily.json`. Multiple concurrent sessions are handled safely with file locking.
4. **At midnight** (or rather, on the first prompt of a new day), the previous day's totals are archived to `~/.claude/statusline_history.jsonl` and the daily counter resets.

No cron jobs, no daemons, no manual intervention. You just use Claude Code and the data accumulates.

### Data files

| File | Purpose | Format |
|------|---------|--------|
| `~/.claude/statusline_daily.json` | Today's running totals | JSON object with per-session and daily aggregates |
| `~/.claude/statusline_history.jsonl` | Historical daily log | One JSON line per day, appended automatically |
| `~/.claude/statusline_quota_cache.json` | Cached API quota data | JSON with 5-minute TTL |

All files are created with owner-only permissions (`0600`). Example history entry:

```json
{"date": "2026-02-18", "input": 2797805, "output": 693769, "cache_read": 1901548, "cache_write": 312000, "sessions": 12}
```

## Step counter (shareable summaries)

`stepcount.py` generates copy-pasteable usage summaries from the accumulated history — like a fitness tracker for AI coding.

```bash
python3 stepcount.py           # today + week + month stacked (default)
python3 stepcount.py -d        # today only
python3 stepcount.py -w        # last 7 days only
python3 stepcount.py -m        # last 30 days only
python3 stepcount.py -t        # today + week + month as ASCII table
python3 stepcount.py --copy    # copy output to clipboard
```

Add `--rough-energy-estimate` to any view to include the order-of-magnitude energy guess.

Week and Month rows only appear once you have 7 and 30 days of data respectively.

**Optional: auto-print after each session.** Add a [Stop hook](https://docs.anthropic.com/en/docs/claude-code/hooks) to `~/.claude/settings.json`:

```json
"hooks": {
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python3 '/path/to/stepcount.py'"
        }
      ]
    }
  ]
}
```

Example output (default):

```
⚡ Claude Code
   Today   18M tokens ·   9 sessions
   Week   563M tokens · 106 sessions
   Month  917M tokens · 156 sessions
```

Example output (`-t`):

```
⚡ Claude Code
   ┌───────┬────────┬──────┬────────────┐
   │       │ tokens │ sess │            │
   ├───────┼────────┼──────┼────────────┤
   │ Today │    18M │    9 │ █░░░░░░░░░ │
   │ Week  │   563M │  106 │ ██████░░░░ │
   │ Month │   917M │  156 │ ██████████ │
   └───────┴────────┴──────┴────────────┘
```

## Results & Claims

This section separates what we can measure with high confidence from what we can only estimate at order-of-magnitude level.

### What we measure (data source)

The script reads Claude Code's **statusbar JSON payload**, piped to stdin on every status update. This payload contains:

- **Per-call snapshot (`current_usage`):** `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` — these reflect the most recent API call. The monitor accumulates these across calls (detecting call boundaries) to build daily totals.
- **Current-context totals:** `total_input_tokens` (= `input + cache_creation + cache_read` of the most recent response) and `total_output_tokens` (that response's output). **As of Claude Code v2.1.122 these are current-context snapshots, not cumulative session counters.** Earlier builds of this monitor treated them as cumulative; the accumulation now sums per-call `current_usage` fields and derives fresh (uncached) input as `total_input − cache_read − cache_creation`.

Daily totals are accumulated across sessions via a locked JSON file. The statusbar payload and
Claude Code's JSONL transcripts are complementary local sources whose coverage depends on the
Claude Code version and workload. In a later v2.1.226 comparison, deduplicated transcript output
was within 1% of the statusline output for a full observed day, while the statusline missed
subagent and non-interactive calls. Neither source should be treated as universally complete (see
[Known limitations](#known-limitations) below).

### Token accounting claims (high confidence, validated)

These claims are supported by a [validation harness](analyze_tokens.py) that logged raw statusbar payloads (Feb 2026: 31 calls / 3 sessions; re-validated May 2026 on CC v2.1.157), plus [direct API billing reconciliation](FINDINGS.md):

1. **No double-counting.** Energy applies separate constants to four non-overlapping token types: fresh (uncached) input, cache reads, cache creation, and output. Fresh input is derived per call as `total_input − cache_read − cache_creation` (≈ `current_usage.input_tokens`).
2. **Thinking visibility is version-dependent.** In the February 2026 captures, the statusbar
   output was roughly 3x the deduplicated JSONL output, and those JSONL records did not expose
   thinking in `usage.output_tokens`. This was a dated observation, not a universal multiplier or
   rule: on Claude Code v2.1.226, deduplicated transcript output was within 1% of statusline
   output for a full observed day.
3. **Cache metrics are accurate.** Per-call `cache_read_input_tokens` and `cache_creation_input_tokens` match API billing to the token across 4 direct API test calls, and are stable within a call (the monitor sums them once per call).
4. **Most prefill work shows up as cache creation.** In a heavily-cached workload, truly-fresh input is tiny (~1% of energy); the bulk of prefill work is `cache_creation`. This is why `E_CW` is treated as roughly prefill-cost.

> ⚠️ **Schema change (CC v2.1.122):** `total_input_tokens`/`total_output_tokens` became *current-context snapshots* rather than cumulative session counters. Builds of this monitor before the 2026-05 fix mis-counted input (≈53× over) and output (under) as a result. See [docs/energy-constants.md](docs/energy-constants.md#2026-05-30-audit-update) for the corrected accounting and re-validation.

Full evidence in [FINDINGS.md](FINDINGS.md). To collect your own validation data, set `ENERGY_DEBUG=1` as an env var in the statusline command, then run `python3 analyze_tokens.py` after a session.

### Energy estimate claims (order-of-magnitude proxy)

The energy numbers shown in the statusbar are **order-of-magnitude estimates, not measurements**. They use per-token energy constants derived from published research and refined via adversarial debate (see [methodology](#energy-estimation-methodology) below), applied to each token type:

```
Energy = (fresh_input × 0.39) + (output × 1.40) + (cache_read × 0.015) + (cache_write × 0.49)  Wh per 1k tokens
```

The display snaps to order-of-magnitude steps (1, 2, 5, 10, 20, 50, ...) because the real uncertainty is at least ±3x in each direction. This is intentionally coarse — it reflects genuine uncertainty, not imprecision in the token counting.

**Which token type dominates energy depends heavily on the workload.** For long interactive sessions (e.g. a Feb 2026 Opus month, ~757M tokens, ~48 kWh mid) **output** dominated (~46% of energy from ~3% of tokens, since decode is ~3.6x more expensive per token than parallel prefill). For many short automated/headless sessions (the more recent pattern) **cache creation** dominates (~34–40%), because each session writes a fresh cache that is read few times before expiring. Don't assume one fixed breakdown — the statusline reflects whatever mix you actually run.

### What the energy estimate does NOT include

- **Full datacenter overhead.** The estimates cover GPU/accelerator compute only — not cooling, networking, CPU/RAM, storage, idle power, or PUE. Google reported a [2.4x multiplier](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) from chip-active to full-stack for Gemini queries. The real operational energy is likely 1.5–3x higher than what this script shows.
- **Training energy.** Training a frontier model costs tens of gigawatt-hours, but that's a one-time cost amortized across millions of users.
- **Embodied energy.** Manufacturing GPUs, building datacenters, networking infrastructure.
- **Your own hardware.** Your laptop and monitor also consume energy while you wait for responses.

### Known limitations

1. **Pricing ≠ energy.** Anthropic's pricing ratios were the original basis for relative energy cost between token types. We've since revised the output and cache read constants using physics-derived cross-checks (FLOP-based estimates, AI Energy Score benchmarks, Google's measured per-query energy). The fresh input and cache write constants still inherit from pricing. Pricing reflects margin, competitive positioning, and demand management — not just energy.

2. **Per-model constants are approximate.** The current implementation uses rough order-of-magnitude
   weights — Haiku ×0.3, Sonnet ×0.6, Opus ×1.0 — as a model for this monitor, based on the
   input-price ratio (1:3:5) discounted for sub-linear parameter-to-energy scaling. These are
   project assumptions, not universal energy multipliers or measurements. Anthropic discloses no
   parameter counts. (Earlier versions used the same constant for all three tiers.)

3. **Context-length decode scaling.** The formula uses a fixed per-output-token constant regardless of context length. With very long cached contexts (25M+ tokens observed in practice), decode cost increases due to larger KV-cache attention. The formula underestimates in exactly these long-context sessions.

4. **Infrastructure variability.** We don't know Anthropic's hardware (GPU types, cluster config), batch sizes, scheduling strategies, model sizes, or datacenter locations. Inference efficiency is a rapidly moving target — Google reported a [33x improvement](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) in a single year.

5. **Source coverage and fields vary by version and workload.** In the February 2026 captures,
   JSONL had streaming placeholder values for `usage.input_tokens` (75% were ≤1 in that dataset)
   and did not expose thinking in `usage.output_tokens`. On Claude Code v2.1.226, a full observed
   day of deduplicated transcript output was within 1% of statusline output, but statusline
   collection missed subagent and non-interactive calls. Neither source is a universal billing
   ledger. Streaming records must be deduplicated by `requestId`, taking the maximum of each token
   field. These comparisons measure local source coverage; they are not API billing
   reconciliation. See [GitHub issue #28197](https://github.com/anthropics/claude-code/issues/28197).

### How to use these numbers responsibly

**Appropriate uses:**
- Awareness — understanding the general scale of compute behind AI-assisted coding
- Relative comparison — "today was a heavier compute day than yesterday"
- Order-of-magnitude budgeting — "our team's AI usage is in the X kWh/day range"
- Motivating efficiency — choosing smaller models for simple tasks, being mindful of long-context sessions

**Not appropriate:**
- Precise carbon accounting or ESG reporting (the uncertainty is too large)
- Comparing energy efficiency between AI providers (the constants are derived from one provider's data)
- Claiming exact energy figures without stating the ±3x uncertainty range

## Energy estimation methodology

### The problem

**No one outside Anthropic knows the actual energy per token for Claude models.** There are no published measurements. Rather than pretending to have precise numbers, the script shows a range with ~10x total uncertainty.

### Mid estimates

The constants use a hybrid approach: Couch's (2026) base estimates from [Epoch AI's GPT-4o research](https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use), revised via adversarial debate (Claude vs Codex, Feb 2026) using physics-derived cross-checks.

| Token type | Mid estimate | Derivation |
|------------|-------------|------------|
| Fresh input (prefill) | 390 mWh/1k tokens | Epoch AI long-context anchor (unchanged from Couch) |
| Output (decode) | 1,400 mWh/1k tokens | Reduced from 1,950; cross-checks cluster 600–1,800 |
| Cached input (cache read) | 15 mWh/1k tokens | Reduced from 39; physics-derived ~26x discount vs input |
| Cache creation (write) | 490 mWh/1k tokens | Prefill + write overhead, ~1.25x fresh input (unchanged) |

The display shows order-of-magnitude estimates (snapping to 1/2/5 per decade) because the real uncertainty is at least ±3x in each direction.

### Why output tokens cost ~3.6x more than input

Input tokens are processed in parallel (prefill), while output tokens are generated one at a time (autoregressive decode). This serial generation is inherently less efficient. The original 5:1 ratio (from Anthropic's pricing) was revised down to ~3.6:1 based on FLOP-based estimates, AI Energy Score benchmarks, and measured Llama 405B inference data, which cluster around 1,000–1,800 mWh/1k output tokens.

### Why cached tokens are ~26x cheaper

Claude Code aggressively caches conversation context. When tokens are read from cache, they skip the expensive prefill computation entirely — the cost is primarily loading pre-computed KV pairs from memory. Anthropic's pricing gives a 10x discount, but physics analysis shows the real compute saving is much larger (potentially 100–1,000x). The 26x discount is a conservative compromise: it accounts for the near-zero compute cost of cache loading plus the ongoing attention cost during decode over cached context.

### Why cache creation costs ~1.25x fresh input

When tokens are written to cache for the first time, they require the same prefill computation as fresh input *plus* the overhead of writing to cache storage. Anthropic charges a 25% surcharge for cache creation, which Couch uses as a proxy for the additional energy cost.

### Cross-checks against published measurements

The estimates were sanity-checked against multiple independent data points:

- **Google** measured [0.24 Wh per median Gemini query](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) (August 2025) — comprehensive, including idle capacity and PUE
- **OpenAI** reported [0.34 Wh per average ChatGPT query](https://blog.samaltman.com/the-gentle-singularity) (June 2025)
- **Couch** derived 41 Wh per median Claude Code session (January 2026) — based on JSONL logs which undercount by ~2.8x (see [FINDINGS.md](FINDINGS.md))
- **AI Energy Score** benchmarks: ~600 mWh/1k output tokens for 70B models, scaled to ~1,200 for 200B+
- **FLOP-based estimate**: 750–1,500 mWh/1k output tokens for a 200B-class model with datacenter overhead
- **Llama 405B measured** (batched): ~2,800 mWh/1k output tokens including overhead

Rationale for each constant in [`docs/energy-constants.md`](docs/energy-constants.md). Full debate transcripts are in `debate/` (gitignored due to size).

### Why the uncertainty is so large

We don't know:
- Anthropic's hardware (GPU types, cluster configuration)
- Batch sizes and scheduling strategies
- Model size (parameter count)
- Inference optimization stack
- Geographic location of datacenters

Inference efficiency is also a rapidly moving target. Google reported a [33x improvement](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) in a single year.

## Everyday comparisons

To put the numbers in context:

| Activity | Energy |
|----------|--------|
| One Google search | ~0.3 Wh |
| One ChatGPT/Gemini query | ~0.3 Wh |
| Charging a smartphone | ~15 Wh |
| LED bulb for 1 hour | 10 Wh |
| Electric oven for 1 minute | 50 Wh |
| Driving an EV 1 km | ~150 Wh |
| Swedish household daily use (with electric heating) | ~40 kWh |

A typical day of AI-assisted coding likely falls in the 1–5 kWh range (mid estimate). A heavy month (119 sessions on Opus 4.6) estimated at ~48 kWh, roughly equivalent to running a fridge for a month. See [FINDINGS.md](FINDINGS.md) for detailed analysis.

## Platform support

| Feature | macOS | Linux | Windows/WSL |
|---------|-------|-------|-------------|
| Token tracking | Yes | Yes | Yes |
| Energy estimates | Yes | Yes | Yes |
| Daily history | Yes | Yes | Yes |
| Prompt cache tracking | Yes | Yes | Yes |
| Durable history and TokenAtlas | Yes | Yes | Yes** |
| API quota display | Yes | Yes* | Yes* |

\* Quota now comes primarily from the statusline payload's `rate_limits` fields (Claude Code v2.1.80+, Pro/Max), which work on every platform with no API call. The legacy fallback reads the OAuth token from the macOS Keychain via the `security` command and is macOS-only; on Linux/Windows it's simply skipped. So if your build provides `rate_limits`, quota shows everywhere; otherwise it's macOS-only.

\** The packaged CLI installs `tzdata` on native Windows so named IANA timezones such as
`Europe/Stockholm` remain available. WSL normally uses the distribution's timezone database.

**Note:** The fallback quota path uses an **undocumented** Anthropic beta API endpoint (`/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`), which may change or disappear without notice. The token and energy features do not depend on it.

## Dependencies

The statusline script uses only the Python 3 standard library (`json`, `os`, `sys`, `subprocess`,
`time`, `fcntl`, `pathlib`, `datetime`). The packaged history CLI is also standard-library-only on
macOS, Linux, and WSL; native Windows installs the small `tzdata` package for named timezones.

**`fcntl` note:** The file locking uses `fcntl.flock`, which is available on macOS and Linux. On Windows (outside WSL), this would need to be replaced with an alternative locking mechanism.

## Security

- The OAuth token is read from macOS Keychain and sent only to `api.anthropic.com`. It is never written to disk.
- On POSIX systems, data files are created with `0600` permissions (owner read/write only). Native
  Windows uses the current user profile's filesystem ACLs because POSIX owner/mode checks are unavailable.
- The script makes no network calls other than the optional quota fetch to Anthropic.
- No telemetry, no third-party services, no analytics.

**Risk:** If someone modifies `~/.claude/statusline.py`, they get code execution in your user context on every Claude Code update. Same threat model as a shell alias or git hook. Keep the file owner-only writable.

## Token counting: validated semantics

We built a [validation harness](analyze_tokens.py) that logs raw statusbar payloads and analyzes token-counting behavior across API calls (Feb 2026: 31 calls / 3 sessions; re-validated May 2026 on CC v2.1.157). Key findings:

- **`total_input_tokens` = `input + cache_creation + cache_read` of the most recent response** (current-context, *not* cumulative, since CC v2.1.122). Fresh (uncached) input is recovered as `total_input − cache_read − cache_creation`.
- **`total_output_tokens` = the most recent response's output** (per-call, not cumulative since v2.1.122). The February 2026 captures showed a roughly 3x statusbar-to-JSONL output ratio, but that ratio and the associated thinking-token interpretation are version/date-specific; v2.1.226 matched within 1% for a full observed day after requestId deduplication. The monitor accumulates `current_usage.output_tokens` per call.
- **Cache fields are stable within a call** and summed once per detected call boundary; no double-counting across the four token types.

To collect your own validation data, set `ENERGY_DEBUG=1` as an env var in the statusline command, then run `python3 analyze_tokens.py` after a session.

Full investigation details in [FINDINGS.md](FINDINGS.md).

## References

### Primary sources for energy estimates

| Source | Year | Description |
|--------|------|-------------|
| [Couch, "Claude Code's Environmental Impact"](https://www.simonpcouch.com/blog/2026-01-20-cc-impact/) | 2026 | Derived per-token energy for Claude from Epoch AI data and Anthropic pricing ratios. Base for fresh input and cache write constants. |
| [Epoch AI, "How much energy does ChatGPT use?"](https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use) | 2025 | Empirical analysis of GPT-4o inference energy |
| [Google, "Measuring the environmental impact of AI inference"](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) | 2025 | Google's own measurements: 0.24 Wh per median Gemini query, 33x efficiency improvement in one year |
| [AI Energy Score v2](https://huggingface.co/spaces/AIEnergyScore/Leaderboard) | 2025 | Standardized inference energy benchmarks on H100 hardware; used to cross-check output constant |
| [Altman, "The Gentle Singularity"](https://blog.samaltman.com/the-gentle-singularity) | 2025 | OpenAI's reported 0.34 Wh per average ChatGPT query |

### Additional academic references

| Source | Year | Description |
|--------|------|-------------|
| [Luccioni et al., "Power Hungry Processing"](https://arxiv.org/abs/2311.16863) | 2024 | Systematic measurement of inference energy across model sizes and tasks |
| [Husom et al., "The Price of Prompting"](https://arxiv.org/abs/2407.16893) | 2024 | Analysis of energy costs for LLM prompting strategies |
| [AI Energy Score v2](https://huggingface.co/blog/sasha/ai-energy-score-v2) | 2025 | Documents 150-700x energy overhead for reasoning/chain-of-thought modes |

## License

[MIT](LICENSE)

## Author

Magnus Gille — [gille.ai](https://gille.ai)

Built collaboratively with Claude Opus 4.6, with a 2026-05 accuracy audit (token-accounting fix for CC v2.1.122, per-model multipliers, refreshed energy literature) by Claude Opus 4.8. Energy estimates, comparisons, and arithmetic independently verified by OpenAI Codex against DOE, ENERGY STAR, IEA, and Swedish Energy Agency sources.

## TokenAtlas: standalone offline dashboard

Create an interactive Swedish report from the retained local history (Python standard library only):

```bash
tokenatlas --db /path/to/history.sqlite3 report --html tokenatlas.html
tokenatlas --db /path/to/history.sqlite3 report --html private.html --private
open -a Safari private.html
```

The default HTML pseudonymizes projects, sessions, turns and agents. `--private` retains short unique
project labels and session references. Neither mode embeds prompts, tool text, source paths, raw usage
objects or machine IDs. Exact timestamps and model metadata remain in shared reports: pseudonymization
is not guaranteed anonymity. Shared reports show a model name verbatim only when it is in a public model family under a public provider; a custom endpoint that reuses a public provider id with a family-like model name is shown, so review a shared report before sending it. JSON from the original `--records` command remains a private diagnostic
export and has different privacy semantics.

The report works without a server, network, CDN or runtime model calls. Filters cover dates, harness,
provider, model, effort, project, session, agent and thread type. Click a timeline bar to zoom from day
to hour/minute, or a ranking to filter. Expand sessions and turns for individual observations. CSV/JSON
exports preserve the current selection, and SVG/print exports support presentations. Top rankings
retain an Other subtotal. Empty intervals are not claimed to be measured zeros. Large timelines group
consecutive observed buckets with an explicit note; the horizontal spacing is categorical. Cache-read
share comparisons show the highest and lowest comparable session, harness, model, and project with
observation count, input volume, and explicit excluded-group coverage.

Unknown counters remain unknown, ambiguous identities are excluded from the known subtotal, and
reasoning is normalized as a subset of output even when a harness stores it additively. No invoice,
quota, wall time or quality is inferred. Source coverage
is separate from arithmetic completeness. Collector revisions now invalidate file checkpoints so
existing retained source files are reparsed after parser fixes; lost source files cannot be recovered.

Checks: `python3 -m unittest discover -v` and `python3 test_interactive_export.py`. The release suite
also builds and installs a wheel in an isolated environment. Optional local browser
check: `PLAYWRIGHT_MODULE=/path/to/@playwright/test node test_report_browser.cjs /absolute/report.html`.
Use an existing Playwright installation and its bundled browser; no browser dependency is installed
by these commands. The browser test blocks network access and checks filters, totals, zoom, drilldown,
exports and mobile overflow.

## Demo data and screenshots

`scripts/demo.py` regenerates the product-page screenshots from entirely fictional data:

```
python3 scripts/demo.py OUTDIR [--seed N] [--no-screens] [--shared]
```

It builds a temporary HOME with synthetic Claude Code, Codex, Pi and OpenCode logs (about 30 days in September 2026, three fictional projects under `/Users/demo/code`, dummy lorem-style content), runs `python3 -m tokenatlas` against it (`refresh`, `overhead --refresh`, `session demo-orchestrated`, `report --html`) and writes `demo-report.html`, `session.txt`, `overhead.txt`, `demo-summary.json` and, unless `--no-screens`, `overview.png`, `session.png` and `overhead.png`. Real logs and state are never read. The HTML report keeps the fictional project labels by default; `--shared` redacts them. The screenshots need Playwright (`PLAYWRIGHT_MODULE`, else a cached npx copy) via `scripts/demo_screens.cjs`.
