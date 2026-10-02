"""ChatGPT credit equivalents for OpenAI/Codex usage: what the tokens correspond to on OpenAI's published credit rate card, never what was drawn
(credits are drawn only after the plan's included usage). Computed offline from credits.json; a missing rate or unknown token count yields no
credits (None), never a guess.

Token mapping (history.normalize, Codex): input_tokens is inclusive of cached input, so tokens['fresh_input'] = input - cached - cache_write and
tokens['cache_read'] = cached input; tokens['output'] is output_tokens, which already contains the reasoning tokens (tokens['reasoning'] is a
subset of it and is not read here). Hence
    credits = fresh_input x input + cache_read x cached_input + output x output      (rates per 1M tokens)
The card has no cache-write rate: a request with cache-write tokens is left out and counted. Speed: not recorded or standard is the card's rate; fast
(speed or service tier 'fast') is the card's fast multiplier (2x, "where available") on every rate; any other speed or tier, including GPT-6 Astra
Ultrafast (6x on the page, but the logs do not identify it), has no usable rate and is left out and counted.
"""
import functools
import json
from pathlib import Path

PACKAGED = Path(__file__).with_name('credits.json')
_TOP_KEYS = ('schema', 'source_url', 'retrieved_on', 'unit', 'speed', 'fast_multiplier', 'provider_aliases', 'models')
_MODEL_KEYS = ('provider', 'model', 'name', 'input', 'cached_input', 'output')
RATE_KEYS = ('input', 'cached_input', 'output')
ASSUMED_STANDARD = 'speed not recorded; counted as standard'
FAST = 'fast speed; counted at the fast multiplier of the standard credit rate'


def _rate(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def load_credits(path=None):
    """The packaged table by default; ValueError naming the model on any schema violation."""
    table = json.loads(Path(path or PACKAGED).read_text())
    if not isinstance(table, dict) or table.get('schema') != 1:
        raise ValueError('credit table: schema must be 1')
    for key in _TOP_KEYS:
        if key not in table:
            raise ValueError(f'credit table: missing key {key}')
    if not isinstance(table['models'], list):
        raise ValueError('credit table: models must be a list')
    if not _rate(table['fast_multiplier']) or table['fast_multiplier'] < 1:
        raise ValueError('credit table: fast_multiplier must be a number >= 1')
    for entry in table['models']:
        name = entry.get('model') if isinstance(entry, dict) else repr(entry)
        if not isinstance(entry, dict):
            raise ValueError(f'{name}: model entry must be an object')
        for key in _MODEL_KEYS:
            if key not in entry:
                raise ValueError(f'{name}: missing key {key}')
        for key in RATE_KEYS:
            if not _rate(entry[key]):
                raise ValueError(f'{name}: {key} must be a number >= 0')
    return table


@functools.lru_cache(maxsize=1)
def packaged():
    return load_credits()


def _result(status, credits=None, model=None, assumptions=(), reason=None, label=None):
    return {'status': status, 'credits': credits, 'model': model, 'assumptions': list(assumptions), 'reason': reason, 'label': label}


def _resolve(obs, table):
    """(early result, None) or (None, (entry, assumptions)) once the row is known to be credit-rated at standard speed."""
    provider = obs.get('provider') or 'unknown'
    provider = (table.get('provider_aliases') or {}).get(provider, provider)
    if provider != 'openai':
        return _result('other_provider'), None
    model = obs.get('model')
    name = model if isinstance(model, str) and model not in ('', 'unknown') else None
    entry = next((e for e in table['models'] if e['provider'] == provider and (e['model'] == name or name in (e.get('aliases') or ()))), None)
    if entry is None:
        return _result('unrated', model=name, reason=f'no credit rate for {name or "an unrecorded model"}'), None
    tariff = obs.get('tariff') or {}
    speed, tier = tariff.get('speed'), tariff.get('service_tier')
    marks = [f'{k}={v}' for k, v in (('speed', speed), ('service_tier', tier)) if v not in (None, 'standard')]
    if not marks:
        return None, (entry, 1, [] if 'standard' in (speed, tier) else [ASSUMED_STANDARD])
    if all(m.endswith('=fast') for m in marks):
        return None, (entry, table['fast_multiplier'], [FAST])
    return _result('nonstandard', model=entry['model'], reason=f'no credit rate at {marks[0]}', label=marks[0]), None


def credit_observation(obs, table):
    """{'status': 'credited' | 'other_provider' | 'unrated' (model without a rate, or cache-write tokens) | 'nonstandard' | 'partial' (an
    input/cached/output count is unknown), 'credits': float or None, 'model': canonical model id when known, 'assumptions', 'reason', 'label' (the speed/tier of a nonstandard row)}."""
    early, found = _resolve(obs, table)
    if early:
        return early
    entry, mult, assumptions = found
    t = obs.get('tokens') or {}
    if (t.get('cache_write') or 0) > 0:
        return _result('unrated', model=entry['model'], reason='cache-write tokens have no credit rate')
    if any(t.get(k) is None for k in ('fresh_input', 'cache_read', 'output')):
        return _result('partial', model=entry['model'], assumptions=assumptions, reason='unknown token classes')
    credits = mult * (t['fresh_input'] * entry['input'] + t['cache_read'] * entry['cached_input'] + t['output'] * entry['output']) / 1e6
    return _result('credited', credits, entry['model'], assumptions)


def credit_vector(obs, table):
    """[input, cached input, output] credits per 1M tokens (the fast multiplier applied) when credit_observation would give credits, else None (for the report payload)."""
    r = credit_observation(obs, table)
    if r['status'] != 'credited':
        return None
    entry, mult, _ = _resolve(obs, table)[1]
    return [mult * entry[k] for k in RATE_KEYS]


def fmt(x):
    """Credits as text: whole numbers from 100, one decimal from 1, two below (the page uses the same rule)."""
    return f'{x:,.0f}' if x >= 100 else f'{x:,.1f}' if x >= 1 else f'{x:,.2f}'
