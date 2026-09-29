# Pricing

Costs are **API-equivalent at list price**: what the recorded tokens would cost at public per-token
prices. They are not what was paid (subscriptions, credits, discounts and batch pricing are not modelled).

## Status values

`usage.pricing.price_observation` returns one status per observation:

- `priced`: every token class has a price and no assumption was needed.
- `assumed`: fully priced, but a tariff dimension was not recorded and standard was assumed.
- `free`: the table marks the model free; cost is 0.
- `local`: the provider is listed in `local_providers`; no cost.
- `partial`: some part is unknown (missing token count or price, cache write TTL unknown, context size
  unknown for a long-context model). Known parts are kept; `cost` is None.
- `unpriced`: no model or list price, or a required tariff price (fast mode, `inference_geo=us`) is missing.

Only `priced`, `assumed` and `free` contribute to a cost total; everything else lowers `coverage`.
A missing price or dimension is never defaulted or zeroed. Costs are summed per currency, never across.

## Rules

- Each class is priced separately: fresh input, cache read, cache write, output. Reasoning tokens are part of
  output and are never priced again. Cache is not inside `fresh_input`.
- Claude cache writes need the 5m/1h split from `raw_usage.cache_creation`; without it, writes above zero make the
  observation `partial`.
- Long context: when a model has `long_context` and total input exceeds `above_input_tokens`, the long prices
  replace the whole price set for that request. Total input is `raw_usage.input_tokens` for Codex/OpenAI and
  fresh + cache read + cache write otherwise.
- Claude `speed=fast` uses the `speed=fast` replacement prices; `inference_geo=us` applies its `multiplier`.
  Fast mode combined with long context is `unpriced`.
- Assumptions: Claude with no recorded `speed` is priced as standard (`speed not recorded; priced as standard`);
  other harnesses record no tariff and are priced as standard (`service tier not recorded; priced as standard`).

## Updating the table

Edit `tokenatlas/prices.json` (schema 1, USD/EUR per million tokens). Give each model its `source_url` and
`retrieved_on`, and keep the top-level `retrieved_on` current. Run `python3 -m unittest test_pricing`; `load_prices`
rejects negative prices and missing keys. History is never re-priced silently: costs are computed at report time
from the table in use, and reports show that table's `retrieved_on`.

## Models without a published list price (checked 2026-09-29)

These show as `unpriced` with reason `no list price for <provider>/<model>`:

- `openai/codex-auto-review`: no row on the OpenAI pricing page, no model page; Codex documents Auto-review
  only as a feature.
- `openai/gpt-5.3-codex-spark`: no row on the pricing page; the model page returns 404.
- `berget/openai/gpt-oss-120b`, `berget/zai-org/GLM-4.7`: Berget AI publishes subscription plans, not
  per-token prices, for these (both retired in September 2026).

Local providers (`m5`, `ollama`, `inference-gille`) have no API-equivalent list price and show as `local`.
