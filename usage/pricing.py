"""API-equivalent list-price costs for history observations (never what was actually paid).

Costs are computed at report time from a price table; a missing price or tariff dimension yields an unknown
(None) part, never a default or zero.  Reasoning tokens are a subset of output and are not priced separately.
"""
import json
from pathlib import Path

PACKAGED = Path(__file__).with_name('prices.json')
PRICE_KEYS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_write', 'cache_read', 'output')
_TOP_KEYS = ('schema', 'retrieved_on', 'unit', 'provider_aliases', 'local_providers', 'models')
_MODEL_KEYS = ('provider', 'model', 'currency', 'input', 'cache_read', 'output', 'free', 'source_url', 'retrieved_on')
_CLASSES = ('fresh_input', 'cache_read', 'cache_write', 'output')
_COVERED = ('priced', 'assumed', 'free')
_STANDARD_CLAUDE = 'speed not recorded; priced as standard'
_STANDARD_OTHER = 'service tier not recorded; priced as standard'


def _price(value):
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0)


def _check_prices(name, where, prices, keys):
    if not isinstance(prices, dict):
        raise ValueError(f'{name}: {where} must be an object')
    for key, value in prices.items():
        if key not in keys and key != 'above_input_tokens' and key != 'multiplier':
            raise ValueError(f'{name}: unknown key {where}.{key}')
        if not _price(value):
            raise ValueError(f'{name}: {where}.{key} must be a number >= 0 or null')


def load_prices(path=None):
    """The packaged table by default; ValueError naming the model on any schema violation."""
    table = json.loads(Path(path or PACKAGED).read_text())
    if not isinstance(table, dict) or table.get('schema') != 1:
        raise ValueError('price table: schema must be 1')
    for key in _TOP_KEYS:
        if key not in table:
            raise ValueError(f'price table: missing key {key}')
    if not isinstance(table['models'], list):
        raise ValueError('price table: models must be a list')
    for entry in table['models']:
        name = f"{entry.get('provider')}/{entry.get('model')}" if isinstance(entry, dict) else repr(entry)
        if not isinstance(entry, dict):
            raise ValueError(f'{name}: model entry must be an object')
        for key in _MODEL_KEYS:
            if key not in entry:
                raise ValueError(f'{name}: missing key {key}')
        _check_prices(name, 'prices', {k: entry[k] for k in PRICE_KEYS if k in entry}, PRICE_KEYS)
        if not isinstance(entry['free'], bool):
            raise ValueError(f'{name}: free must be a boolean')
        if entry.get('long_context') is not None:
            _check_prices(name, 'long_context', entry['long_context'], PRICE_KEYS)
            if not isinstance(entry['long_context'].get('above_input_tokens'), int):
                raise ValueError(f'{name}: long_context.above_input_tokens must be an integer')
        for label, modifier in (entry.get('modifiers') or {}).items():
            _check_prices(name, f'modifiers.{label}', modifier, PRICE_KEYS)
    return table


def _find(table, provider, model):
    provider = (table.get('provider_aliases') or {}).get(provider, provider)
    for entry in table.get('models', ()):
        if entry['provider'] == provider and (entry['model'] == model or model in (entry.get('aliases') or ())):
            return provider, entry
    return provider, None


def _result(status, parts=None, cost=None, currency=None, assumptions=(), reason=None, ref=None):
    return {'cost': cost, 'currency': currency, 'status': status,
            'parts': parts or dict.fromkeys(('input', 'cache_write', 'cache_read', 'output')),
            'assumptions': list(assumptions), 'reason': reason, 'price_ref': ref}


def _times(count, price):
    """Tokens x price per million; zero tokens cost nothing whatever the price, unknown stays unknown."""
    if count is None:
        return None
    if count == 0:
        return 0.0
    return None if price is None else count * price / 1e6


def price_observation(obs, table):
    provider, model = obs.get('provider') or 'unknown', obs.get('model')
    provider = (table.get('provider_aliases') or {}).get(provider, provider)
    if provider in (table.get('local_providers') or ()):
        return _result('local', reason='local model')
    if not isinstance(model, str) or model in ('', 'unknown'):
        return _result('unpriced', reason=f'no model recorded for {provider}')
    provider, entry = _find(table, provider, model)
    if entry is None:
        return _result('unpriced', reason=f'no list price for {provider}/{model}')
    ref = {'provider': provider, 'model': entry['model'], 'source_url': entry.get('source_url'),
           'retrieved_on': entry.get('retrieved_on')}
    currency = entry.get('currency')
    if entry.get('free'):
        return _result('free', dict.fromkeys(('input', 'cache_write', 'cache_read', 'output'), 0.0), 0.0, currency, ref=ref)
    tokens, raw = obs.get('tokens') or {}, obs.get('raw_usage') or {}
    claude = obs.get('harness') == 'claude'
    prices = {k: entry.get(k) for k in PRICE_KEYS}
    assumptions, reasons, multiplier = [], [], 1.0

    def unpriced(reason):
        return _result('unpriced', reason=reason, currency=currency, assumptions=assumptions, ref=ref)

    # Long context replaces the whole price set once the request's total input exceeds the threshold.
    long = entry.get('long_context')
    context_unknown = long_applies = False
    if long:
        # fresh + cache read + cache write is the request's whole input in every harness (Codex's inclusive
        # input_tokens equals it); fall back to the raw inclusive count when a class is unknown.
        known = [tokens.get(k) for k in ('fresh_input', 'cache_read', 'cache_write')]
        total = raw.get('input_tokens') if None in known else sum(known)
        if total is None:
            context_unknown = True
            reasons.append('context size unknown')
        elif total > long['above_input_tokens']:
            long_applies = True
            prices = {k: long.get(k) for k in PRICE_KEYS}
    modifiers = entry.get('modifiers') or {}
    if claude:
        tariff = obs.get('tariff') or {}
        speed = tariff.get('speed')
        if speed == 'fast':
            if 'speed=fast' not in modifiers:
                return unpriced('fast mode price missing')
            if long_applies:
                return unpriced('fast mode with long context is not priced')
            prices = {k: modifiers['speed=fast'].get(k) for k in PRICE_KEYS}
        elif speed is None:
            assumptions.append(_STANDARD_CLAUDE)
        elif speed != 'standard':
            return unpriced(f'unknown speed {speed}')
        geo, tier = tariff.get('inference_geo'), tariff.get('service_tier')
        if geo == 'us':
            if 'multiplier' not in (modifiers.get('inference_geo=us') or {}):
                return unpriced('inference_geo=us price missing')
            multiplier = modifiers['inference_geo=us']['multiplier']
        elif geo not in (None, 'not_available', 'global'):
            return unpriced(f'unknown inference_geo {geo}')
        if tier not in (None, 'standard'):
            if f'service_tier={tier}' not in modifiers:
                return unpriced(f'unknown service_tier {tier}')
            if speed == 'fast' or long_applies:
                return unpriced(f'service_tier {tier} with fast mode or long context is not priced')
            prices = {k: modifiers[f'service_tier={tier}'].get(k) for k in PRICE_KEYS}
    else:
        assumptions.append(_STANDARD_OTHER)
    if context_unknown:
        prices = dict.fromkeys(PRICE_KEYS)

    write = tokens.get('cache_write')
    if claude:
        split = raw.get('cache_creation') or {}
        five, hour = split.get('ephemeral_5m_input_tokens'), split.get('ephemeral_1h_input_tokens')
        if five is not None and hour is not None and write is not None and five + hour == write:
            pair = (_times(five, prices['cache_write_5m']), _times(hour, prices['cache_write_1h']))
            write_part = None if None in pair else sum(pair)
        else:
            write_part = 0.0 if write == 0 else None
            if write:
                reasons.append('cache write TTL unknown')
    else:
        write_part = _times(write, prices['cache_write'])
    parts = {'input': _times(tokens.get('fresh_input'), prices['input']), 'cache_write': write_part,
             'cache_read': _times(tokens.get('cache_read'), prices['cache_read']),
             'output': _times(tokens.get('output'), prices['output'])}
    if multiplier != 1.0:
        parts = {k: None if v is None else v * multiplier for k, v in parts.items()}
    missing = [k for k, v in parts.items() if v is None]
    if missing:
        if not reasons:
            reasons.append('unknown or unpriced token classes: ' + ', '.join(missing))
        return _result('partial', parts, None, currency, assumptions, '; '.join(reasons), ref)
    return _result('assumed' if assumptions else 'priced', parts, sum(parts.values()), currency, assumptions, None, ref)


def summarize_costs(observations, table, key=lambda o: (o['provider'], o['model'])):
    """Per group: token totals, cost per currency, status counts, coverage and the price refs used."""
    groups = {}
    for obs in observations:
        result = price_observation(obs, table)
        group = groups.setdefault(key(obs), {
            'key': key(obs), 'observations': 0, 'tokens': dict.fromkeys(_CLASSES, 0), 'cost': {}, 'status': {},
            'coverage': None, 'unpriced_tokens': 0, 'price_refs': [], '_known': 0, '_covered': 0})
        group['observations'] += 1
        known = 0
        for name in _CLASSES:
            value = (obs.get('tokens') or {}).get(name)
            if value is not None:
                group['tokens'][name] += value
                known += value
        group['status'][result['status']] = group['status'].get(result['status'], 0) + 1
        group['_known'] += known
        if result['status'] in _COVERED:
            group['_covered'] += known
            group['cost'][result['currency']] = group['cost'].get(result['currency'], 0.0) + result['cost']
        else:
            group['unpriced_tokens'] += known
        if result['price_ref'] and result['price_ref'] not in group['price_refs']:
            group['price_refs'].append(result['price_ref'])
    for group in groups.values():
        known, covered = group.pop('_known'), group.pop('_covered')
        group['coverage'] = covered / known if known else None
    return list(groups.values())
