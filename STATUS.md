# Project Status

**Last session:** 2026-04-08
**Branch:** master

## Completed This Session (2026-04-08)
- **Fixed stale statusline.py deployment** — deployed `~/.claude/statusline.py` was from Mar 1, missing all quota analyzer features (per-session model/project tracking, daily deltas, stale pruning, session history archiving). Per-model and per-project breakdown in `advisor.py --breakdown` was showing "?" for all sessions.
- **Symlinked statusline.py** — `~/.claude/statusline.py` → repo copy. No more manual deploy step; edits are live immediately.
- **Data collection confirmed active** — 47 days of history, Pi sync running, per-model/project data will populate from next API call onward.

## Completed This Session (2026-03-29)
- **Quota analyzer (`advisor.py --breakdown`)** — new per-project/per-model cost breakdown view. Shows top sessions by quota impact, output token share analysis. Per-session daily deltas (`di/do/dc/dcw`) now tracked in statusline.py. Stale baselines (sessions with no metadata) pruned at midnight.
- **Pi headless energy monitoring — implemented and deployed**:
  - `pi_scanner.py` — scans print-mode JSONL from `~/.claude/projects/` on Pi. Dual mode: `--file` for targeted parsing, global scan for cron catch-up. requestId dedup, fcntl locking, schema warnings.
  - `pi_sync.sh` — rsync wrapper syncing journal + rollup from Pi to laptop.
  - `advisor.py` + `stepcount.py` extended to merge Pi data (additive per date, `--no-pi` flag).
  - **Deployed:** Scanner running via cron `*/15` on Pi (`huginmunin`). Sync running via cron `*/30` on laptop. Initial scan found 51 sessions across 7 days.
  - Pi sessions now appear in advisor analysis (model mix shows Opus/Haiku, projects show heimdall/hugin/skuld).
- **Pi implementation adversarial debate** (2 rounds, 12 critique points):
  - Dropped `last-prompt` as completion gate (replaced with "assistant entry with nonzero usage")
  - Changed from rollup-only to full journal sync
  - Confirmed requestId dedup needed even in print mode
  - Scanner is decoupled v1; Hugin-side capture is target architecture

## Completed This Session (2026-03-27)
- **Sonnet-first pilot experiment** — wrote `docs/sonnet-pilot-experiment.md`: 3-phase evaluation (baseline/pilot/evaluate) of routing Cat A+B Hugin tasks to Sonnet. Phase 1 starts today (Mar 27 - Apr 2).
- **Notification pipeline** — tested Telegram (Ratatoskr) and email (Heimdall) channels. Ratatoskr `POST /api/send` endpoint added via Hugin task (ae4f64a). Email deprecated: `grimnir-bot@outlook.com` flagged by Microsoft (AADSTS70000 "service abuse"). Set `NOTIFY_ENABLED=false`.
- **Heimdall EnvironmentFile debate** — 2-round Codex debate on fixing missing `MICROSOFT_MCP_CLIENT_ID`. Applied fix (`EnvironmentFile=/home/magnus/.heimdall/env`), but discovered real root cause is the Microsoft account lockout, not just the missing env var. MSAL cache has 2 accounts; `accounts[0]` happens to be correct (`grimnir-bot`) but is fragile.
- **Email deprecation across repos** — updated grimnir `docs/architecture.md`, heimdall `STATUS.md`, Munin `projects/heimdall/status` and `people/grimnir-bot/status`. All notifications now go through Telegram.
- **Free email research** — evaluated 8 transactional email services. Best free options: Brevo (300/day, no domain needed) or SMTP2GO (200/day). Parked for now — Telegram is sufficient.
- **Session history + advisor tooling** (40bcd18) — statusline.py now tracks per-session metadata (model, project, context size, cost, API calls). advisor.py provides analysis.

## Previous Session
- Codex monitor suite (codex_status.py, codex_stepcount.py, codex_with_summary.py)
- Model tiering and headless energy monitoring debates

## In Progress
- **Per-model/project data collection** — symlink deployed, awaiting first full day of data to verify breakdown works. Backup at `~/.claude/statusline.py.bak` (remove once confirmed).

## Deployment: Pi Energy Scanner
- **Pi cron:** `*/15 * * * * python3 /home/magnus/repos/claude-code-energy-monitor/pi_scanner.py` (logs to syslog via `logger -t pi-energy-scanner`)
- **Laptop cron:** `*/30 * * * * /Users/magnus/repos/claude-code-energy-monitor/pi_sync.sh` (logs via `logger -t pi-energy-sync`)
- **Pi data files:** `~/.claude/pi_journal.jsonl` (append-only, 51 entries), `~/.claude/pi_daily_rollup.jsonl` (derived daily totals)
- **Laptop copies:** same filenames in `~/.claude/`, synced via rsync
- **To update scanner:** `scp pi_scanner.py huginmunin.local:~/repos/claude-code-energy-monitor/pi_scanner.py`

## Next Steps
- **Sonnet-first pilot** — baseline week in progress (Mar 27 - Apr 2). Set up scheduled trigger for daily monitoring + Telegram alerts.
- **Model tiering for Hugin** (debate Round 2 completed 2026-03-26):
  - If implemented, do not flip all code tasks to Sonnet by default without telemetry
  - First add a minimal append-only invocation journal from the first pilot run
  - Define a narrow acceptance contract for a small pilot task set before broader Sonnet-first rollout
  - Treat `opusplan` as research only until Sonnet-first pilot data shows a planning gap
- Per-model energy constants (Haiku/Sonnet/Opus use same constants despite ~2-5x size differences)
- Improve active-session heuristics if Codex changes rollout behavior or adds a native status hook
- Consider blog post about JSONL placeholder finding (issue #28197)
- Context-length-dependent input constants
- Gemini CLI support — still research only
- PyPI packaging — defer until multi-CLI or broader adoption
