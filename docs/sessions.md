# Session trees and outcomes

`energy-monitor session <id>` reads the saved history (no source logs, no network) and prints one
root session as a tree, with tokens and API-equivalent cost per node and per model.
Costs are list-price equivalents, not what was paid. Unknown cost prints `n/a`, never 0; a `≥`
marks a lower bound (unfinal Claude subagent output, incomplete rows, or unpriced observations).
Node figures include children; the root main thread is also shown as *coordination* (conductor overhead).

## How links are derived

- `root`: the main thread of the given session (`harness:id` when the id exists in several harnesses).
- `path`: Claude subagents share the parent session id; they are grouped by agent id (from the `agent-<id>.jsonl` source file, not the agent type) under the
  root and labelled `<type> <id first 8>`.
  Workflow agents (`subagents/workflows/wf_<run>/`) sit under a `workflow wf_<run>` node.
- `explicit`: sessions whose `parent_session` names a node (Codex, OpenCode, Claude), recursively.
- `inferred`: headless children (origin `codex_exec`, `exec`, `sdk-cli`, `sdk-py`, `sdk-ts`) with no
  parent, whose whole time span lies within the root span plus 10 minutes and whose cwd equals,
  contains or lies under the root cwd. Headless launches carry no link to their launcher, so this is
  a heuristic; `--no-infer` turns it off.
- `unassigned`: a candidate that would fit another non-headless main session as well (ambiguous), and
  parentless subagent sessions (for example a Codex guardian) inside the window and cwd. They are
  listed with the reason and never added to the totals. A guessed link is never made silently.

## Outcomes file

`outcomes.jsonl` next to the history database (or `--outcomes PATH`), one JSON object per line:

```json
{"v":1,"root_session":"…","unit":"name","threads":[{"harness":"claude","agent_id":"…"}],
 "outcome":"pass","note":"","ts":"2026-09-10T10:00:00Z"}
```

Threads are `{"harness","session"}`, `{"harness":"claude","agent_id"}` or `{"harness":"claude","workflow_run"}`.
Outcomes are `pass`, `partial`, `redo`, `wrong`. Legacy `v: 0` lines are read too (extra keys ignored, lines
without `outcome` skipped). Malformed lines are counted and reported, never fatal. If a unit is rated
twice the last line wins.

`energy-monitor rate <id>` lists the thread keys (`claude:agent:<id>`, `claude:workflow:<run>`,
`<harness>:<session>`); `rate <id> --unit U --thread KEY [--thread KEY…] --outcome pass [--note T]`
appends one line (mode 0600, single append) and prints it.

## Efficiency metric

Per model: all tokens and cost of every rated unit that the model took part in (including partial,
redo and wrong attempts) divided by the number of `pass` units it took part in, with n = units per
outcome. This is the same definition as cost per passed result. With zero passes the value is `n/a`.
Unrated threads are listed as unrated; the root main thread is coordination, not a unit. Ratings are
never guessed.

## Limits

A single session is descriptive, not a ranking: n is tiny and tasks differ. Claude subagent output is
a lower bound (`output_not_final`), so their cost per pass is too. Inferred links can be wrong and
unlinked work is excluded from the tree.
