# Project Status

**Last session:** 2026-06-24
**Branch:** master

## Completed This Session (2026-06-24) — external calculator comparison (aifootprintcalculator.org)

Analysis session (no code changes). Helped fill in an external SV energy calculator with this user's real CC usage, then reverse-engineered its method from its JS bundle and compared to ours.

- **Real 30-day usage (rollup, 2026-05-26..06-24):** output 31.7M, fresh input 8.0M, cache_read 5.77B, cache_write 140M tokens; ~3,400 prompts (estimated — only 8 days of raw transcripts survive nightly cleanup, JSONL `input_tokens` are placeholders so rollup is the better monthly source). Our model on this usage: **~2,000–2,500 kWh/yr** (Opus-anchored mult=1.0 → 2,467; fleet-mix ~0.82 → 2,023). Cache = ~77% of our estimate (read 43% + write 34%).
- **aifootprintcalculator.org says 30,307 kWh/yr — ~12–15× our full estimate, ~50× on a per-token basis.** Internally consistent (0.02 kWh/charge × 30,307 ≈ 1.515M phone charges shown).
- **Their method (extracted from `assets/index-*.js`):** top-down per-request. `gpuKwh = avgWhPerStandardRequest × prompts × Sr(out,22/7) × Sr(in,292/173)/1000`, `total = gpuKwh × 2.44` (PUE/overhead), `co2 = total × regionFactor` (Sweden 10.83, Anthropic-blended 509 gCO2e/kWh). `Sr(x,k)=1+(x−300)/600×(k−1)`, standard request = 300/300 tok. Per-model anchors from published data (Claude Opus ≈ 4.05 Wh/std-req, Sonnet ≈ 0.85). No cache term. Training amortization optional, off by default. Output:input weight 3.1× (ours 3.6×).
- **Root cause of the gap:** (1) **multiplicative input×output coupling** (`n·r`) — at ~9.4k out/2.4k in per prompt the input factor 3.35× re-multiplies the whole request → 112× a standard request; no physical basis, explodes on long-context agentic prompts; (2) global **×2.44 overhead** we omit; (3) higher base anchor (~7× our GPU/300-tok figure). Their model is implicitly calibrated for short chat turns.
- **Actionable finding for us:** the **×2.44 PUE/overhead** is a legitimate critique of our GPU-only model — we likely understate *delivered* energy (lit. PUE ~1.4–2.0; +non-GPU ~2.4). Their per-named-model published anchors are also better-sourced than our 3-tier multiplier guesses. Their lack of any cache term conversely **confirms our core thesis** (cache-aware accounting is the agentic-tool differentiator). Offered but NOT yet done: (a) `docs/` comparison note + FINDINGS.md entry, (b) prototype optional PUE factor.

## Completed This Session (2026-06-17) — API-cost reconciliation + filed issue #4

Analysis session (no code changes). Question: "what would the last 30 days have cost via API instead of the Max sub?"

- **Settled answer: ~$3,650 / 30 days** (band $3.5–4k) — ~18× effective discount vs the ~$200 Max sub, *not* the 80–250× a naive transcript estimate implied. Cost is ~96% cache reads (billed 0.1×); split ~$2.0k over the light pre-ramp 22 days + ~$1.7k over the heavy last 8 days (post Max ×20 upgrade).
- **Reconciled two wildly different estimates** (rollup-based ~$3.1k/30d vs another agent's transcript-based ~$11k/9d → implied $20–38k/30d) via a multi-agent workflow + adversarial verify. Found **two opposite-direction measurement defects**:
  - **(A) Rollup undercounts ~15%.** `update_daily()` is fed only main-session per-render snapshots → subagent/workflow (Task/Workflow) calls contribute **zero**, and calls between renders are dropped (`captures_subagent: no`, `captures_workflow: no`).
  - **(B) Naive transcript summation overcounts 2.21×.** CC writes one JSONL record per content block (thinking/text/each tool_use), all sharing one `requestId` + identical `message.usage`. 40,973 blocks → 19,257 real calls (53% dupes). Raw cache_read 5.12B → 2.32B deduped. **Must dedup by `requestId`.**
- **Filed [issue #4](https://github.com/Magnus-Gille/claude-code-energy-monitor/issues/4)** capturing both fixes (rollup subagent-capture path via `sum_jsonl.py` reconciliation + document the requestId-dedup requirement in FINDINGS.md). Added to Grimnir Roadmap board.
- Both defects were latent: (A) was flagged 2026-03-31 (Munin `enhancements-cache-analysis`) and never built; (B) is a third JSONL pitfall not yet in FINDINGS.md (which documents the opposite-direction input/output undercount).

## Completed This Session (2026-05-31) — fix context percentage display

- **Removed `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`** from `~/.claude/settings.json`. The flag was set in Apr 2026 to cap context to 200k for cost control. In CC v2.1.158 it no longer caps real usage (debug log showed `total_input_tokens` routinely reaching 400–600k, peak 598k) but still forces `context_window_size: 200000` in the statusline payload. Result: `used_percentage` was computed against a denominator 3–5× too small, pinning to 100% whenever actual usage exceeded 200k.
- **No statusline.py changes needed** — `Ctx:%` is read verbatim from the payload. After removing the flag and restarting CC, Claude Code should report a 1M window and the percentage will be accurate.
- **Follow-up (low priority):** could harden `statusline.py` to recompute `ctx_pct = total_input_tokens / context_window_size` directly so a future label/reality mismatch can't silently mislead.

## Completed This Session (2026-05-30) — accuracy audit + fixes → **v1.0.0**
Deep-dive audit (Claude Opus 4.8, CC v2.1.157): multi-agent research workflow (49 agents, adversarial verification) + `ENERGY_DEBUG` re-validation. **Merged to master via PR #3 (squash `6aa2619`), Codex-reviewed, and cut as the repo's first tagged release `v1.0.0`.**

- **Token accounting fix (critical).** CC **v2.1.122** redefined `context_window.total_input_tokens`/`total_output_tokens` from cumulative session counters to **current-context snapshots** (`total_input = input+cache_creation+cache_read` of latest response; `total_output` = that response's output, resets per call). The old `update_daily` delta logic was over-counting fresh input **~53×** (tracking context growth) and under-counting output; cache terms unaffected. **Rewrote `update_daily`** to accumulate per-call `current_usage`, detecting call boundaries (input-side change OR output reset — also fixes the consecutive-identical-fully-cached-call miss). Fresh input = `total_input − cache_read − cache_creation`. **Replay-validated against ground truth on 392 captured fires: exact match on all 4 token types.** Fresh-input energy share drops from spurious ~12–15% → ~1%.
- **Per-model multipliers** (decision): Haiku ×0.3 / Sonnet ×0.6 / Opus ×1.0 (price-proxy 1:3:5, discounted for sub-linear scaling; order-of-magnitude). statusline weights today + future via `d['by_model']`; legacy history days fall back to 1.0×. stepcount mirrors with a Pi residual term. Fleet mix: Opus 56% / Sonnet 43% / Haiku 1%.
- **E_CW** (decision): value kept at 490, **relabeled** "prefill + pricing-derived infra surcharge proxy" (cache creation = prefill FLOPs; +25% has no measured energy basis).
- **Quota source**: `/api/oauth/usage` (now 429-dead) → statusline payload `rate_limits.{five_hour,seven_day}.used_percentage` (CC v2.1.80+), OAuth as fallback.
- **Constants unchanged in value** (390/1400/15/490): new 2026 evidence (Oviedo/*Joule* 0.34 Wh + 4–20× overstatement; ML.ENERGY v3.0 + B200 −35%; Jegham Claude-3.7) all sit within ±3× and bracket E_OUT. **E_CACHE is the most leverage-sensitive constant** (cache_read volume ~23× cache_write). Workload inverted Feb→May: output-dominated → cache_write-dominated (~40%), driven by a 23% drop in cache_read reuse (not volume, +9%).
- **Docs**: FINDINGS.md ~5 kWh → ~9 kWh arithmetic fix + audit section; README Opus 4.6→4.8, corrected validated-semantics claims, workload-dependent breakdown; new `docs/energy-constants.md` "2026-05-30 audit update". Noted Opus 4.7+ tokenizer (+0–35% tokens) and Opus 4.8 cache-min 4096→1024.
- **Files**: statusline.py, energy_constants.py, stepcount.py, pi_scanner.py, README.md, FINDINGS.md, docs/energy-constants.md. All compile; statusline replay/e2e/smoke-tested; advisor + stepcount run clean on live data.

## Completed This Session (2026-04-18)
- **Token optimization analysis** — advisor.py + breakdown shows Opus at 89-93% of weekly quota; 9 sessions hit >80% ctx; 257 extended-context sessions (now capped via CLAUDE_CODE_DISABLE_1M_CONTEXT=1, already set on both laptop and Pi).
- **Pinned 4 skills to Sonnet** (close, capture, index-artifacts, submit-task) — added Model Check step: "stop and ask the user to run /model sonnet, wait for confirmation before proceeding." Also pinned submit-task. Reviewed by Codex CLI (PR #1) — wording clarified to make delegation explicit.
- **Deleted duplicate `/index-artifacts` command** — `~/.claude/commands/index-artifacts.md` was stale (old `~/mgc/` path). Deleted from both laptop and Pi.
- **Skills repo PR #1 merged** — `github.com/Magnus-Gille/claude-skills`. Established PR workflow (direct push to main is blocked). Codex cross-model review integrated.
- **grimnir-bot added as collaborator on claude-skills** — Pi's GitHub identity; was blocked from pulling. Invite accepted, Pi remote switched to SSH, `git pull` now works.

## Completed This Session (2026-04-10)
- **plot_daily.py improvements** (3a53332) — stacked bars (compute vs cache), gap-filling for missing days, 7-day trailing average (causal, no edge artifacts), correct window semantics. Identified input undercounting bug on low-activity days and cache_write growth trend as monitor improvement signals.
- **Memory/CLAUDE.md audit and cleanup** — full audit of all 24 project MEMORY.md files vs Munin. Migrated 8 valuable local-only entries to Munin (gavel, hoganas-coach, flowdictate, 4× feedback/hugin, munin-zero hardware). Deleted 12 redundant/duplicate files. Trimmed 9 MEMORY.md files (munin-memory 204→25 lines, AXON 243→20, claude-code-energy-monitor 136→50, etc.). Total reduction: ~1,800 lines → ~600 lines across all project memories.
- **CLAUDE.md global trim** — Munin section 94→55 lines (-40%). Removed redundant prose, verbose Desktop/Web/Mobile instructions, artifact indexing sub-sections. All rules preserved.
- **Removed MEMORY.md auto-memory reference from CLAUDE.md** — no longer instructs Claude to maintain local MEMORY.md files alongside Munin.
- **claude.ai MCP note** — the 3 dead claude.ai MCPs (M365, Gmail, Calendar) are account-synced from claude.ai web, not CLI-managed. Must be removed via claude.ai → Settings → Integrations.

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
