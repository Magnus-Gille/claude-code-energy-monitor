# Sonnet-First Pilot Experiment

## Hypothesis

Switching Hugin code tasks from Opus (default) to Sonnet will meaningfully reduce Max subscription quota consumption without a significant increase in task failure rate.

## Background

All Hugin tasks currently run on the Pi's default model (Opus). Anthropic docs confirm Opus and non-Opus models have separate weekly quota tracking, implying different quota weights. The exact ratio is unknown.

## Task Classification

Based on 25 completed + 5 failed historical Hugin tasks, tasks fall into these categories:

### Category A: Ops/Admin (Sonnet-safe)
Quick, well-specified tasks with clear success criteria.
- Pull-and-restart deployments (e.g., heimdall-redeploy)
- Service health checks (e.g., email-final-test)
- Git operations (e.g., push-hugin)
- Version checks, installs (e.g., update-claude, install-codex)
- Config edits (e.g., uncomment-anthropic-key-in-env)

**Typical profile:** <5 min, <300s timeout, exit code is sufficient for success detection.

### Category B: Scoped Implementation (Sonnet candidate)
Code tasks with clear specs and bounded scope.
- UI card fixes (e.g., fix-hugin-tasks, hide-retried-failures, humanize-tasks-events)
- Add specific feature to existing codebase (e.g., hugin-email-delivery, task-queue-failed-card)
- CI pipeline setup (e.g., munin-ci-pipeline)
- Debug specific issue (e.g., debug-hugin-card)

**Typical profile:** 5-30 min, clear prompt with specific deliverables, tests/lint available for acceptance.

### Category C: Complex/Ambiguous (Opus or opusplan)
Tasks requiring architectural judgment, multi-concern reasoning, or ambiguous specs.
- Dream project (open-ended brainstorm + full MVP implementation)
- Quality signals & intelligence (multi-feature, failed at 2h timeout)
- A2A pivot research + repositioning (research + code, failed)
- Visual overhaul/redesign (subjective quality criteria)

**Typical profile:** >30 min, ambiguous success criteria, high failure risk.

## Experiment Design

### Phase 1: Baseline Collection (1 week, no changes)

The journal is already active. Continue submitting tasks as normal (Opus default) to establish baseline metrics:

- Tasks per day
- Quota delta per task (capture 5h% before and after each task)
- Success/failure rate
- Duration distribution

**Quota capture method:** Before each task, record `q5` from `~/.claude/statusline_quota_cache.json`. After task completes, re-fetch quota. The delta is the task's quota impact. Imprecise (laptop usage is concurrent), but directionally useful over multiple samples.

### Phase 2: Sonnet Pilot (2 weeks)

Submit Category A and B tasks with `**Model:** claude-sonnet-4-6`. Keep Category C tasks on Opus (default or explicit `**Model:** claude-opus-4-6`).

### Phase 3: Evaluate and Expand

Based on data, either expand Sonnet-first to more task types, try `opusplan` for Category C, or revert.

## Evaluation Criteria

### Primary: Quota Impact

| Metric | How to measure | Decision threshold |
|--------|---------------|-------------------|
| Quota delta per Sonnet task | 5h% before minus 5h% after | Must be measurably lower than Opus baseline |
| Weekly quota headroom | 7d% at end of week | Must show improvement vs baseline week |

**Success:** Sonnet tasks consume detectably less quota than equivalent Opus tasks.
**Abort:** No measurable difference after 10+ task pairs, OR 7d% doesn't improve.

### Secondary: Task Quality

| Metric | How to measure | Decision threshold |
|--------|---------------|-------------------|
| Success rate (Cat A) | Exit code 0 / total | Must remain >90% (baseline ~100%) |
| Success rate (Cat B) | Exit code 0 AND acceptance checks pass | Must remain >70% (baseline ~80%) |
| Escalation rate | Tasks re-submitted on Opus after Sonnet failure | <30% of Cat B tasks |

**Success:** Comparable success rate with <30% escalation.
**Abort:** >50% Cat B failure rate, OR >3 consecutive Cat B failures.

### Tertiary: Efficiency

| Metric | How to measure | Decision threshold |
|--------|---------------|-------------------|
| Duration | Journal `duration_s` | Sonnet <2x Opus for same task category |
| Cost (API-equivalent) | Journal `cost_usd` | Informational only (not billed) |

### Acceptance Checks for Cat B Tasks

Add to task prompts where applicable:

```markdown
## Acceptance Criteria
Before committing, verify:
1. `npm run build` (or equivalent) succeeds with no errors
2. `npm test` (if tests exist) passes
3. `git diff --stat` shows changes in expected files only
4. No lint errors introduced
```

## Notifications

Experiment alerts are delivered via **Telegram** (Ratatoskr `POST http://localhost:3034/api/send` on the Pi). A scheduled trigger checks experiment progress daily and sends a message when: a phase boundary is reached, an abort condition is triggered, or manual input is needed.

## Data Collection

### Invocation Journal (already active)
`~/.hugin/invocation-journal.jsonl` — one line per task:
```json
{"ts":"...","task_id":"...","repo":"...","model_requested":"claude-sonnet-4-6","exit_code":0,"duration_s":180,"cost_usd":0.42}
```

### Quota Snapshots (new — add to submit-task skill)
Before submitting, record quota. After task completes, record again. Store in journal or Munin.

**Implementation:** Add a pre/post quota capture to the orchestration. The simplest approach: the `check-tasks` skill reads `q5`/`q7` when checking results and logs it.

### Analysis Script
Extend `advisor.py` to read the Pi's invocation journal (via SSH or synced file) and produce:
- Per-model success rate
- Per-model average quota delta (if quota snapshots available)
- Per-category breakdown
- Escalation history

## Task Assignment Rules

During the pilot:

| Task type | Model | Rationale |
|-----------|-------|-----------|
| Cat A: ops/admin | `claude-sonnet-4-6` | Low risk, clear success criteria |
| Cat B: scoped implementation | `claude-sonnet-4-6` | Primary test surface |
| Cat B: scoped impl + retry | `claude-opus-4-6` | Escalation after Sonnet failure |
| Cat C: complex/ambiguous | *(default/Opus)* | Not part of pilot |
| Cat C: complex + decomposable | `opusplan` | Exploratory (3-5 tasks) |

## Timeline

| Week | Activity |
|------|----------|
| Week 1 (Mar 27 - Apr 2) | Baseline: journal active, all tasks on Opus, capture quota deltas |
| Week 2-3 (Apr 3 - Apr 16) | Pilot: Cat A+B on Sonnet, Cat C on Opus, track all metrics |
| Week 4 (Apr 17 - Apr 23) | Evaluate: analyze journal, compare quota impact, decide on expansion |

## Decision Framework

After the pilot:

| Outcome | Action |
|---------|--------|
| Sonnet saves >20% quota, <30% escalation | Default all Cat A+B to Sonnet. Update submit-task skill. |
| Sonnet saves quota but >30% escalation | Use Sonnet for Cat A only. Try opusplan for Cat B. |
| No measurable quota difference | Revert to Opus default. Model tiering adds no value for Max sub. |
| Sonnet fails >50% of Cat B tasks | Revert immediately. Sonnet inadequate for Pi tasks. |

## Risks

1. **Concurrent laptop usage confounds quota measurement** — mitigate by running Pi tasks during low-laptop-activity periods when possible
2. **Sample size too small** — 2 weeks may yield only 10-15 tasks. Accept that initial data will be noisy.
3. **Task heterogeneity** — no two tasks are identical. Compare within categories, not across.
4. **Quota API instability** — the undocumented `/api/oauth/usage` endpoint could change. Journal data (cost, duration) is the fallback metric.
