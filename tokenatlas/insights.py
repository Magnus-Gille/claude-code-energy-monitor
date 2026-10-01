"""Cost facts: deterministic, rule-based statements computed from the observations and the price table; no model, no interpretation.

Every fact is {id, title_key, values, computation, assumptions, provenance, ...}: `values` are the numbers, `computation` and `assumptions`
say how they were computed (English text from report_i18n.json 'en', the keys are kept for the page's own languages), and `provenance`
is 'measured' (counted from the logs) or 'computed' (arithmetic on measured values with the price table). A fact that cannot be computed
reliably, or has no data, is omitted. Costs are list prices in USD (see pricing); requests without a complete USD price are counted, never guessed.
"""
import functools
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path

from tokenatlas import pricing, prompts

I18N = Path(__file__).with_name('report_i18n.json')
BIG_TURN = 50.0
TOP_MODELS, ALTERNATIVES, MIN_SHARE = 5, 3, 0.10
PARTS = ('input', 'cache_write', 'cache_read', 'output')
PREMIUM = ('speed=fast', 'service_tier=fast', 'service_tier=priority')  # flex is a discount, not a premium
COMMON = ('ins_a_list', 'ins_a_scope')


@functools.lru_cache(maxsize=None)
def _texts():
    return json.loads(I18N.read_text(encoding='utf-8'))['en']


def say(key, params):
    return re.sub(r'\{(\w+)\}', lambda m: f'{params[m[1]]:g}' if isinstance(params[m[1]], float) else str(params[m[1]]), _texts()[key])


def _fact(id, values, computation, assumptions, provenance='computed', **params):
    return {'id': id, 'title_key': f'ins_{id}', 'values': values, 'params': params, 'provenance': provenance,
            'computation_key': computation, 'computation': say(computation, params),
            'assumption_keys': list(assumptions), 'assumptions': [say(k, params) for k in assumptions]}


def _usd(res):
    """Cost in USD or None: a non-USD price is never mixed into a USD sum (as in prompts._cost)."""
    c = res['cost']
    return c if c is not None and (res['currency'] == 'USD' or (res['status'] == 'free' and res['currency'] is None)) else None


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _share(part, whole):
    return part / whole if whole else None


def cost_facts(records, table, start=None, end=None, big_turn=BIG_TURN, name=None, memo=None):
    """{'window', 'requests', 'priced_requests', 'unpriced_requests', 'facts': [...]} over observations with start <= ts < end.
    `name(provider, model)` maps a model to its displayed name (the shared report passes its redaction; default: the model itself).
    `memo`, a dict reused across calls with the same records and table, only saves repeated price lookups; it never changes a result."""
    memo = {} if memo is None else memo
    name = name or (lambda provider, model: model)
    start, end = _when(start), _when(end)
    inside = [r for r in records if (start is None or prompts._t(r['ts']) >= start) and (end is None or prompts._t(r['ts']) < end)]
    rows = []  # one per request: (record, result, cost or None, tier)
    for r in inside:
        if id(r) not in memo:
            tier = {}
            res = pricing.price_observation(r, table, tier=tier)
            memo[id(r)] = (r, res, _usd(res), tier)  # r is kept so that its id stays unique
        rows.append(memo[id(r)])
    priced = [x for x in rows if x[2] is not None]
    total = sum(x[2] for x in priced)
    n, k = len(rows), len(priced)
    scope = dict(requests=n, priced_requests=k, unpriced_requests=n - k, unpriced_share=_share(n - k, n))
    facts = []
    if k and total > 0:
        # canonical (provider, model): aliases and the provider alias table collapse into the price table's own entry
        groups = {}
        for r, res, cost, _ in priced:
            ref = res['price_ref']
            g = groups.setdefault((ref['provider'], ref['model']), {'cost': 0.0, 'requests': 0, 'rows': []})
            g['cost'] += cost
            g['requests'] += 1
            g['rows'].append(r)
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1]['cost'], kv[0]))
        shown = [dict(name=name(p, m), cost=g['cost'], share=g['cost'] / total, requests=g['requests']) for (p, m), g in ranked[:TOP_MODELS]]
        rest = ranked[TOP_MODELS:]
        other = dict(models=len(rest), cost=sum(g['cost'] for _, g in rest), share=sum(g['cost'] for _, g in rest) / total,
                     requests=sum(g['requests'] for _, g in rest)) if rest else None
        facts.append(_fact('model_share', dict(models=shown, other=other, priced_cost=total, **scope), 'ins_model_share_c',
                           (*COMMON, 'ins_a_alias')))
        facts += _comparison(ranked, total, table, name)
        parts = {p: sum(x[1]['parts'][p] for x in priced) for p in PARTS}
        facts.append(_fact('cost_parts', dict(parts=[dict(part=p, cost=parts[p], share=parts[p] / total) for p in PARTS], priced_cost=total, **scope),
                           'ins_cost_parts_c', (*COMMON, 'ins_a_reasoning')))
    facts += _context(rows)
    if k and total > 0:
        facts += _long(priced, table, k) + _turns(records, inside, start, end, big_turn, scope, memo) + _subagents(priced, total, k) + _tiers(priced, table, k)
    order = ('model_share', 'price_comparison', 'cost_parts', 'context_size', 'long_context_premium', 'big_turns', 'subagent_share', 'premium_tiers')
    facts.sort(key=lambda f: order.index(f['id']))
    return {'window': {'start': start and start.isoformat(), 'end': end and end.isoformat()}, **{k_: scope[k_] for k_ in ('requests', 'priced_requests', 'unpriced_requests')},
            'big_turn': big_turn, 'facts': facts}


LADDER = 8


def _reprice(rows, provider, model, table):
    """{model: total cost} of the same observations priced at each other model of `provider` that can price all of them (free, non-USD and
    infeasible models are absent). Cost is linear in the token classes once the rate set is fixed, so observations are grouped by everything
    that selects the rates (harness, tariff, whether the 5m/1h cache-write split is known, which candidate long-context thresholds the input
    exceeds), their token classes are summed and each group is priced once per candidate through price_observation, with the long-context
    decision forced to the members' common outcome. `rows` must all be priced USD observations; the result equals per-observation pricing."""
    cands = [e for e in table.get('models', ()) if e['provider'] == provider and e['model'] != model and e.get('currency') == 'USD' and not e.get('free')]
    thresholds = sorted({e['long_context']['above_input_tokens'] for e in cands if e.get('long_context')})
    groups = {}
    for r in rows:
        t = r.get('tokens') or {}
        classes = {k: t.get(k) or 0 for k in ('fresh_input', 'cache_read', 'cache_write', 'output', 'reasoning')}
        total = classes['fresh_input'] + classes['cache_read'] + classes['cache_write']
        cc = (r.get('raw_usage') or {}).get('cache_creation') or {}
        five, hour = cc.get('ephemeral_5m_input_tokens'), cc.get('ephemeral_1h_input_tokens')
        split = five is not None and hour is not None and five + hour == classes['cache_write']
        key = (r.get('harness'), json.dumps(r.get('tariff'), sort_keys=True), split, classes['cache_write'] > 0, tuple(total > x for x in thresholds))
        g = groups.setdefault(key, {'rep': r, 'tokens': dict.fromkeys(classes, 0), 'five': 0, 'hour': 0})
        for k, v in classes.items():
            g['tokens'][k] += v
        if split:
            g['five'] += five
            g['hour'] += hour
    out = {}
    for alt in cands:
        threshold = (alt.get('long_context') or {}).get('above_input_tokens')
        cost = 0.0
        for key, g in groups.items():
            rep = dict(g['rep'], provider=alt['provider'], model=alt['model'], tokens=g['tokens'],
                       raw_usage={'cache_creation': {'ephemeral_5m_input_tokens': g['five'], 'ephemeral_1h_input_tokens': g['hour']}} if key[2] else {})
            crossed = key[4][thresholds.index(threshold)] if threshold is not None else False
            c = _usd(pricing.price_observation(rep, table, long_context='always' if crossed else False))
            if c is None:
                break  # this model cannot price every request: left out
            cost += c
        else:
            out[alt['model']] = cost
    return out


def _ladder(ranked_entry, total, table, name):
    (provider, model), g = ranked_entry
    others = _reprice(g['rows'], provider, model, table)
    if not others:
        return None
    entries = sorted([(c, m, False) for m, c in others.items()] + [(g['cost'], model, True)], key=lambda x: (-x[0], x[1]))
    at = next(i for i, e in enumerate(entries) if e[2])
    first = min(max(at - LADDER // 2, 0), max(len(entries) - LADDER, 0))  # more than LADDER models: the LADDER around the actual one
    return dict(name=name(provider, model), cost=g['cost'], share=g['cost'] / total, requests=g['requests'], ladder_models=len(entries),
                ladder=[dict(name=name(provider, m), cost=c, actual=a) for c, m, a in entries[first:first + LADDER]])


def _comparison(ranked, total, table, name):
    out = [x for x in (_ladder(e, total, table, name) for e in ranked if e[1]['cost'] / total >= MIN_SHARE) if x]
    return [_fact('price_comparison', dict(models=out), 'ins_price_comparison_c', (*COMMON, 'ins_a_samecounts', 'ins_a_alternatives'))] if out else []


def _context(rows):
    by, excluded = {}, 0
    for r, *_ in rows:
        t = r.get('tokens') or {}
        known = [t.get(c) for c in ('fresh_input', 'cache_read', 'cache_write')]
        if None in known:
            excluded += 1
        else:
            by.setdefault(r['harness'], []).append(sum(known))
    if not by:
        return []
    harnesses = [dict(harness=h, requests=len(v), median=float(statistics.median(v)), p90=sorted(v)[math.ceil(0.9 * len(v)) - 1]) for h, v in sorted(by.items())]
    return [_fact('context_size', dict(harnesses=harnesses, excluded_requests=excluded), 'ins_context_size_c', ('ins_a_known', 'ins_a_logs'), 'measured')]


def _long(priced, table, k):
    longs = [x for x in priced if x[3].get('long')]
    if not longs:
        return []
    std = [_usd(pricing.price_observation(x[0], table, long_context=False)) for x in longs]
    if None in std:
        return []  # the standard-tier cost is not computable: no premium is stated
    actual, standard = sum(x[2] for x in longs), sum(std)
    return [_fact('long_context_premium', dict(requests=len(longs), actual=actual, standard=standard, premium=actual - standard, priced_requests=k),
                  'ins_long_context_premium_c', (*COMMON, 'ins_a_othertiers'))]


def _tiers(priced, table, k):
    prem = [x for x in priced if x[3].get('modifier') in PREMIUM]
    if not prem:
        return []
    std = [_usd(pricing.price_observation(x[0], table, modifiers=False)) for x in prem]
    if None in std:
        return []
    actual, standard = sum(x[2] for x in prem), sum(std)
    tiers = {}
    for x in prem:
        tiers[x[3]['modifier']] = tiers.get(x[3]['modifier'], 0) + 1
    return [_fact('premium_tiers', dict(requests=len(prem), actual=actual, standard=standard, extra=actual - standard, tiers=dict(sorted(tiers.items())), priced_requests=k),
                  'ins_premium_tiers_c', (*COMMON, 'ins_a_tier_recorded', 'ins_a_flex'))]


def _subagents(priced, total, k):
    subs = [x for x in priced if x[0].get('thread_kind') == 'subagent']
    if not subs:
        return []
    cost = sum(x[2] for x in subs)
    return [_fact('subagent_share', dict(subagent_cost=cost, total_cost=total, share=cost / total, subagent_requests=len(subs), priced_requests=k),
                  'ins_subagent_share_c', (*COMMON, 'ins_a_subagent'))]


def _turns(records, inside, start, end, big_turn, scope, memo):
    """Turn costs as prompts.top_prompts defines them (assign_prompts over all records; a turn's cost is the sum of its priced requests inside the
    window, a turn without a priced request has none), without building its per-turn detail: that is what makes 200k observations affordable."""
    if 'assigned' not in memo:
        memo['assigned'] = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}  # the same for every window over these records
    turns, unattributed = {}, 0
    for r in inside:
        found = memo['assigned'][id(r)]
        if not found:
            unattributed += 1
            continue
        t = turns.setdefault(found[:3], [0.0, 0, 0])
        t[1] += 1
        cost = memo[id(r)][2]
        if cost is not None:
            t[0] += cost
            t[2] += 1
    costed = [(t[0], t[1]) for t in turns.values() if t[2]]
    big = [x for x in costed if x[0] >= big_turn]
    if not big:
        return []
    attributed, cost = sum(x[0] for x in costed), sum(x[0] for x in big)
    return [_fact('big_turns', dict(threshold=big_turn, count=len(big), turns=len(costed), cost=cost, attributed_cost=attributed, share=cost / attributed,
                                    median_requests=float(statistics.median(x[1] for x in big)), unattributed_requests=unattributed, **scope),
                  'ins_big_turns_c', (*COMMON, 'ins_a_turn_window', 'ins_a_turn_lower', 'ins_a_unattributed'), big_turn=big_turn)]


def public(result):
    """The compact form for the report payload: keys and numbers only (the page has the texts in its own languages)."""
    return {**result, 'facts': [{k: v for k, v in f.items() if k not in ('computation', 'assumptions')} for f in result['facts']]}


def _usd_text(x):
    return f'${x:,.2f}'


def _pct(x):
    return f'{100 * x:.1f}%'


def _signed(x):
    return ('+' if x >= 0 else '-') + _usd_text(abs(x))


def _rq(n):
    return f"{n:,} request" + ('' if n == 1 else 's')


def _lines(f):
    v, i = f['values'], f['id']
    if i == 'model_share':
        out = [f"{m['name']}: {_usd_text(m['cost'])} ({_pct(m['share'])}), {_rq(m['requests'])}" for m in v['models']]
        if v['other']:
            o = v['other']
            out.append(f"other ({o['models']} models): {_usd_text(o['cost'])} ({_pct(o['share'])}), {_rq(o['requests'])}")
        return out + [f"priced cost: {_usd_text(v['priced_cost'])} over {_rq(v['priced_requests'])}",
                      f"without a complete USD list price: {v['unpriced_requests']:,} of {_rq(v['requests'])} ({_pct(v['unpriced_share'])})"]
    if i == 'price_comparison':
        out = []
        for m in v['models']:
            out.append(f"{m['name']}: {_usd_text(m['cost'])} ({_pct(m['share'])} of priced cost); the same tokens at list prices of {len(m['ladder'])} of {m['ladder_models']} models from the same provider, highest first:")
            out += [f"  {x['name']}: {_usd_text(x['cost'])}" + ('  <- model used' if x['actual'] else '') for x in m['ladder']]
        return out
    if i == 'cost_parts':
        return [f"{p['part'].replace('_', ' ')}: {_usd_text(p['cost'])} ({_pct(p['share'])})" for p in v['parts']] + [f"priced cost: {_usd_text(v['priced_cost'])}"]
    if i == 'context_size':
        out = [f"{h['harness']}: median {h['median']:,.0f} tokens, p90 {h['p90']:,} tokens, {_rq(h['requests'])}" for h in v['harnesses']]
        return out + ([f"not counted (an input class is unknown): {_rq(v['excluded_requests'])}"] if v['excluded_requests'] else [])
    if i == 'long_context_premium':
        return [f"requests at the long-context tier: {v['requests']:,} of {v['priced_requests']:,} priced", f"cost at the tier applied: {_usd_text(v['actual'])}",
                f"same requests at the standard tier: {_usd_text(v['standard'])}", f"premium: {_usd_text(v['premium'])}"]
    if i == 'big_turns':
        return [f"turns costing >= {_usd_text(v['threshold'])}: {v['count']:,} of {v['turns']:,} turns with a known turn",
                f"their cost: {_usd_text(v['cost'])} of {_usd_text(v['attributed_cost'])} ({_pct(v['share'])})", f"median requests per such turn: {v['median_requests']:g}"]
    if i == 'subagent_share':
        return [f"cost from subagents: {_usd_text(v['subagent_cost'])} of {_usd_text(v['total_cost'])} ({_pct(v['share'])})", f"requests from subagents: {v['subagent_requests']:,} of {v['priced_requests']:,} priced"]
    return [f"requests at a fast or priority tier: {v['requests']:,} ({', '.join(f'{k} {n:,}' for k, n in v['tiers'].items())})", f"cost at the tier applied: {_usd_text(v['actual'])}",
            f"same requests at the standard tier: {_usd_text(v['standard'])}", f"extra cost: {_usd_text(v['extra'])}"]


def render_text(result):
    """Human-readable English block per fact: the numbers, then the computation and assumptions on indented lines. Aggregates only."""
    w = result['window']
    span = f"{w['start'] or 'the first request'} to {w['end'] or 'now'}" if w['start'] or w['end'] else 'all history'
    out = ['Cost facts: list-price USD, computed locally from saved observations (no language model, no interpretation)',
           f"Window: {span}", f"Requests: {result['requests']:,} ({result['priced_requests']:,} with a complete USD list price, {result['unpriced_requests']:,} without)"]
    if not result['facts']:
        out.append('No cost facts can be computed for this window.')
    for n, f in enumerate(result['facts'], 1):
        out += ['', f"{n}. {_texts()[f['title_key']]} [{f['provenance']}]"] + [f'   {x}' for x in _lines(f)]
        out += [f"   computation: {f['computation']}", '   assumptions:'] + [f'     - {a}' for a in f['assumptions']]
    return '\n'.join(out)
