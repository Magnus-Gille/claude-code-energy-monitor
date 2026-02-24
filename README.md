# Claude Code Energy Monitor

A statusline script for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that shows real-time token usage and estimated energy consumption. It tracks session and daily totals, distinguishes cheap cached tokens from expensive fresh tokens, and logs daily history automatically.

```
Opus 4.6 | Ctx:47% | 5h:29% 7d:52% | S:124k 7-64Wh | D:2.0M 0.5-4.2kWh
```

Reading left to right:

| Segment | Meaning |
|---------|---------|
| `Opus 4.6` | Active model |
| `Ctx:47%` | Context window usage |
| `5h:29% 7d:52%` | API quota consumption (5-hour and 7-day rolling windows) |
| `S:124k 7-64Wh` | Session total tokens and energy range |
| `D:2.0M 0.5-4.2kWh` | Daily total tokens and energy range (across all sessions) |

## Installation

**30-second setup** — paste this into Claude Code:

> Please set up a custom statusline for me. Do the following:
>
> 1. Download the script from this repo and save it to `~/.claude/statusline.py`
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

## Energy estimation methodology

### The problem

**No one outside Anthropic knows the actual energy per token for Claude models.** There are no published measurements. Rather than pretending to have precise numbers, the script shows a range with ~10x total uncertainty.

### Center estimates

The center estimates come from [Simon P. Couch's analysis](https://www.simonpcouch.com/blog/2026-01-20-cc-impact/) (January 2026) of Claude Code energy use. Couch derived per-token energy figures from [Epoch AI's research](https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use) on GPT-4o inference energy and Anthropic's pricing ratios.

| Token type | Low estimate | Center | High estimate |
|------------|-------------|--------|---------------|
| Fresh input (prefill) | 130 mWh/1k tokens | 390 | 1,170 mWh/1k tokens |
| Output (decode) | 650 mWh/1k tokens | 1,950 | 5,850 mWh/1k tokens |
| Cached input (cache read) | 13 mWh/1k tokens | 39 | 117 mWh/1k tokens |
| Cache creation (write) | 163 mWh/1k tokens | 490 | 1,470 mWh/1k tokens |

The low and high bounds are 3x below and above the center (i.e. divide/multiply by 3), giving a ~10x range from low to high.

### Why output tokens cost ~5x more than input

Input tokens are processed in parallel (prefill), while output tokens are generated one at a time (autoregressive decode). This serial generation is inherently less efficient, requiring roughly 5x the energy per token.

### Why cached tokens are ~10x cheaper

Claude Code aggressively caches conversation context. When tokens are read from cache, they skip the expensive prefill computation entirely. The 10x ratio mirrors Anthropic's pricing structure (cached input is 90% cheaper than fresh input).

### Why cache creation costs ~1.25x fresh input

When tokens are written to cache for the first time, they require the same prefill computation as fresh input *plus* the overhead of writing to cache storage. Anthropic charges a 25% surcharge for cache creation, which Couch uses as a proxy for the additional energy cost.

### Validation against published measurements

The range brackets several published data points:

- **Google** measured [0.24 Wh per median Gemini query](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) (August 2025)
- **OpenAI** reported [0.34 Wh per average ChatGPT query](https://blog.samaltman.com/the-gentle-singularity) (June 2025)
- **Couch** derived 41 Wh per median Claude Code session (January 2026)

### Why the range is so wide

We don't know:
- Anthropic's hardware (GPU types, cluster configuration)
- Batch sizes and scheduling strategies
- Model size (parameter count)
- Inference optimization stack
- Geographic location of datacenters

Inference efficiency is also a rapidly moving target. Google reported a [33x improvement](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) in a single year.

### Compute-only vs full datacenter energy

These estimates cover **accelerator compute energy only** — the GPU/TPU work of processing tokens. They do not include the full operational footprint of a datacenter: cooling, networking, CPU/RAM overhead, storage, idle power, and Power Usage Effectiveness (PUE).

Google's [infrastructure measurement blog post](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) (August 2025) quantifies this gap: their median Gemini query goes from 0.10 Wh (chip-active only) to 0.24 Wh (full-stack) — a **2.4x multiplier** just from accounting methodology.

This means the real operational energy could be 1.5-3x higher than what this script shows. The uncertainty band already partially absorbs this (our high estimate uses 3x the center), but for a complete picture you should be aware that "compute energy" and "datacenter electricity" are different things.

## What this does NOT capture

- **Full datacenter overhead.** See above. Our estimates are compute-focused. The real operational footprint is likely 1.5-3x higher.
- **Training energy.** Training a frontier model costs tens of gigawatt-hours, but that's a one-time cost amortized across millions of users.
- **Reasoning mode overhead.** Extended thinking / chain-of-thought can use [150-700x more energy](https://huggingface.co/blog/sasha/ai-energy-score-v2) than standard inference. The estimates above are for standard inference only. However, Claude Code's `total_output_tokens` **does include thinking tokens** (confirmed via validation harness — see [FINDINGS.md](FINDINGS.md)). The statusbar reports ~3x more output tokens than the JSONL conversation logs for well-covered sessions, consistent with Opus's chain-of-thought overhead. This means the energy estimate already scales with reasoning use.
- **Embodied energy.** Manufacturing GPUs, building datacenters, networking infrastructure.
- **Your own hardware.** Your laptop and monitor also consume energy while you wait for responses.

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

A typical day of AI-assisted coding likely falls in the 0.5-5 kWh range. A heavy day on Opus (full workday, many sessions) measured at 3-28 kWh (center ~10 kWh), equivalent to running several extra refrigerators or driving an EV 20-180 km. See [FINDINGS.md](FINDINGS.md) for a detailed analysis of a real usage day.

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

## Known limitations

1. **Model-agnostic constants.** The same energy constants are used for Haiku, Sonnet, and Opus. In practice, Opus likely uses 2-5x more energy per token due to larger model size. This means the estimate may undercount for heavy Opus usage and overcount for Haiku.

2. **Context-length decode scaling.** The energy formula uses a fixed per-output-token constant regardless of context length. With very long cached contexts (25M+ tokens observed in practice), decode cost increases due to larger KV-cache attention. The formula underestimates in exactly these long-context sessions.

3. **JSONL logs are incomplete.** Claude Code's JSONL conversation logs miss tool use intermediate calls, subagent API calls, and context management operations. Tools like [ccusage](https://github.com/ryoppippi/ccusage) that read JSONL may undercount actual compute by 3-15,000x on input/output tokens. This monitor reads the statusbar context, which is the more complete data source.

## References

### Primary sources for energy estimates

| Source | Year | Description |
|--------|------|-------------|
| [Couch, "Claude Code's Environmental Impact"](https://www.simonpcouch.com/blog/2026-01-20-cc-impact/) | 2026 | Derived per-token energy for Claude from Epoch AI data and Anthropic pricing ratios |
| [Epoch AI, "How much energy does ChatGPT use?"](https://epoch.ai/gradient-updates/how-much-energy-does-chatgpt-use) | 2025 | Empirical analysis of GPT-4o inference energy |
| [Google, "Measuring the environmental impact of AI inference"](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference) | 2025 | Google's own measurements: 0.24 Wh per median Gemini query, 33x efficiency improvement in one year |
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
