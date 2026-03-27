# Project Status

**Last session:** 2026-03-27
**Branch:** master

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
- Nothing active

## Next Steps
- **Sonnet-first pilot** — baseline week in progress (Mar 27 - Apr 2). Set up scheduled trigger for daily monitoring + Telegram alerts.
- **Model tiering for Hugin** (debate Round 2 completed 2026-03-26):
  - If implemented, do not flip all code tasks to Sonnet by default without telemetry
  - First add a minimal append-only invocation journal from the first pilot run
  - Define a narrow acceptance contract for a small pilot task set before broader Sonnet-first rollout
  - Treat `opusplan` as research only until Sonnet-first pilot data shows a planning gap
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
