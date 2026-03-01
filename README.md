# Claude Code Energy Monitor

A statusline script for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that shows real-time token usage and order-of-magnitude energy estimates. It tracks daily, weekly, and monthly totals, distinguishes cheap cached tokens from expensive fresh tokens, and logs history automatically.

```
Opus 4.6 | 5h:29% 7d:52% | D:2.0M ~2kWh | W:45.3M ~20kWh | M:412M ~50kWh
```

Reading left to right:

| Segment | Meaning |
|---------|---------|
| `Opus 4.6` | Active model |
| `5h:29% 7d:52%` | API quota consumption (5-hour and 7-day rolling windows) |
| `D:2.0M ~2kWh` | Daily total tokens and energy estimate |
| `W:45.3M ~20kWh` | Weekly total (Monday–today) |
| `M:412M ~50kWh` | Monthly total (1st–today) |

## Installation

**30-second setup** — paste this into Claude Code:

> Please set up a custom statusline for me. Do the following:
>
> 1. Download `statusline.py` from https://github.com/Magnus-Gille/claude-code-energy-monitor and save it to `~/.claude/statusline.py`
> 2. Run `chmod +x ~/.claude/statusline.py`
> 3. Run `claude config set --global statusline "python3 ~/.claude/statusline.py"` to enable it

Or do it manually:

```bash
# Download the script
curl -o ~/.claude/statusline.py https://raw.githubusercontent.com/Magnus-Gille/claude-code-energy-monitor/master/statusline.py

# Make it executable
chmod +x ~/.claude/statusline.py

# Register it with Claude Code
claude config set --global statusline "python3 ~/.claude/statusline.py"
```

That's it. The statusline appears the next time you start a Claude Code session.

## How it works

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

- **Cumulative session totals:** `total_input_tokens`, `total_output_tokens` — these grow monotonically across API calls within a session.
- **Per-call snapshot:** `current_usage.input_tokens`, `current_usage.output_tokens`, `current_usage.cache_read_input_tokens`, `current_usage.cache_creation_input_tokens` — these reflect the most recent API call.
- **Per-call deltas** are derived by detecting when `total_input_tokens` increases (signaling a new API call) and computing the difference from the previous total.

Daily totals are accumulated across sessions via a locked JSON file. This is the most complete token data source available — more complete than JSONL conversation logs (see [Known limitations](#known-limitations) below).

### Token accounting claims (high confidence, validated)

These claims are supported by a [validation harness](analyze_tokens.py) that logged raw statusbar payloads across 31 API calls in 3 concurrent sessions, plus [direct API billing reconciliation](FINDINGS.md):

1. **No double-counting.** `total_input_tokens` counts fresh input only — it excludes cache creation and cache read tokens. The energy formula applies separate constants to each token type without overlap.
2. **Thinking tokens are included.** `total_output_tokens` includes extended thinking (chain-of-thought) tokens, confirmed by a 1.0x ratio to the API's `usage.output_tokens` field (which [includes thinking](https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking#token-usage-and-pricing)) and a ~3x ratio versus JSONL logs (which exclude thinking).
3. **Cache metrics are accurate.** Per-call `cache_read_input_tokens` and `cache_creation_input_tokens` match API billing to the token across 4 direct API test calls.
4. **The energy formula is correct as-is.** Fresh input, cached reads, cache creation, and output are counted separately and completely. No changes needed.

Full evidence in [FINDINGS.md](FINDINGS.md). To collect your own validation data, set `ENERGY_DEBUG=1` as an env var in the statusline command, then run `python3 analyze_tokens.py` after a session.

### Energy estimate claims (order-of-magnitude proxy)

The energy numbers shown in the statusbar are **order-of-magnitude estimates, not measurements**. They use per-token energy constants derived from published research and refined via adversarial debate (see [methodology](#energy-estimation-methodology) below), applied to each token type:

```
Energy = (fresh_input × 0.39) + (output × 1.40) + (cache_read × 0.015) + (cache_write × 0.49)  Wh per 1k tokens
```

The display snaps to order-of-magnitude steps (1, 2, 5, 10, 20, 50, ...) because the real uncertainty is at least ±3x in each direction. This is intentionally coarse — it reflects genuine uncertainty, not imprecision in the token counting.

On a real heavy-usage month (Opus 4.6, 119 sessions, ~757M tokens), the mid estimate was **~48 kWh**. Output tokens dominated energy cost (~43% of energy from just 2% of tokens) because autoregressive decode is ~3.6x more expensive per token than parallel prefill.

### What the energy estimate does NOT include

- **Full datacenter overhead.** The estimates cover GPU/accelerator compute only — not cooling, networking, CPU/RAM, storage, idle power, or PUE. Google reported a [2.4x multiplier](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) from chip-active to full-stack for Gemini queries. The real operational energy is likely 1.5–3x higher than what this script shows.
- **Training energy.** Training a frontier model costs tens of gigawatt-hours, but that's a one-time cost amortized across millions of users.
- **Embodied energy.** Manufacturing GPUs, building datacenters, networking infrastructure.
- **Your own hardware.** Your laptop and monitor also consume energy while you wait for responses.

### Known limitations

1. **Pricing ≠ energy.** Anthropic's pricing ratios were the original basis for relative energy cost between token types. We've since revised the output and cache read constants using physics-derived cross-checks (FLOP-based estimates, AI Energy Score benchmarks, Google's measured per-query energy). The fresh input and cache write constants still inherit from pricing. Pricing reflects margin, competitive positioning, and demand management — not just energy.

2. **Model-agnostic constants.** The same energy constants are used for Haiku, Sonnet, and Opus. Opus likely uses 2–5x more energy per token due to larger model size. The estimate may undercount for heavy Opus usage and overcount for Haiku.

3. **Context-length decode scaling.** The formula uses a fixed per-output-token constant regardless of context length. With very long cached contexts (25M+ tokens observed in practice), decode cost increases due to larger KV-cache attention. The formula underestimates in exactly these long-context sessions.

4. **Infrastructure variability.** We don't know Anthropic's hardware (GPU types, cluster config), batch sizes, scheduling strategies, model sizes, or datacenter locations. Inference efficiency is a rapidly moving target — Google reported a [33x improvement](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) in a single year.

5. **JSONL logs are incomplete.** Claude Code's JSONL conversation logs have streaming placeholder values for `usage.input_tokens` (75% are ≤1) and exclude thinking tokens from `usage.output_tokens`. Tools like [ccusage](https://github.com/ryoppippi/ccusage) that read JSONL may undercount actual compute by 10–174x. This monitor reads the statusbar context, which is the more complete data source. See [GitHub issue #28197](https://github.com/anthropics/claude-code/issues/28197).

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

Full debate transcript and analysis in `debate/energy-constants-summary.md` (gitignored).

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
| API quota display | Yes | No* | No* |

\* The quota feature reads the OAuth token from the macOS Keychain using the `security` command. On Linux and Windows/WSL, the quota segments are silently omitted. Everything else works.

**Note:** The quota display uses an **undocumented** Anthropic beta API endpoint (`/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`). This may change or disappear without notice. The token and energy features do not depend on it.

## Dependencies

None. The script uses only the Python 3 standard library (`json`, `os`, `sys`, `subprocess`, `time`, `fcntl`, `pathlib`, `datetime`).

**`fcntl` note:** The file locking uses `fcntl.flock`, which is available on macOS and Linux. On Windows (outside WSL), this would need to be replaced with an alternative locking mechanism.

## Security

- The OAuth token is read from macOS Keychain and sent only to `api.anthropic.com`. It is never written to disk.
- All data files are created with `0600` permissions (owner read/write only).
- The script makes no network calls other than the optional quota fetch to Anthropic.
- No telemetry, no third-party services, no analytics.

**Risk:** If someone modifies `~/.claude/statusline.py`, they get code execution in your user context on every Claude Code update. Same threat model as a shell alias or git hook. Keep the file owner-only writable.

## Token counting: validated semantics

We built a [validation harness](analyze_tokens.py) that logs raw statusbar payloads and analyzes token-counting behavior across API calls. Key findings (31 API calls across 3 concurrent sessions):

- **`total_input_tokens` = fresh input only.** It excludes cache creation and cache read tokens. No double-counting in the energy formula.
- **`total_output_tokens` includes thinking tokens.** Both the cumulative total and per-call `current_usage.output_tokens` include extended thinking, confirmed by 1.0x ratio between them and ~3x ratio vs JSONL (which excludes thinking).
- **Energy formula is correct as-is.** Fresh input, cached reads, cache creation, and output are counted separately without overlap.

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

Built collaboratively with Claude Opus 4.6. Energy estimates, comparisons, and arithmetic independently verified by OpenAI Codex against DOE, ENERGY STAR, IEA, and Swedish Energy Agency sources.
