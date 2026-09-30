"""Top prompts: roll every request up to the user prompt (turn) that caused it, then rank the prompts."""
from bisect import bisect_right
from datetime import datetime

from tokenatlas.pricing import price_observation

CLASSES = ('fresh_input', 'cache_write', 'cache_read', 'output', 'reasoning')
RESUME = {'claude': 'claude --resume {}', 'codex': 'codex resume {}'}
MAX_DEPTH = 8


def _t(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))


def assign_prompts(records):
    """{observation id: (harness, root_session, turn_id, 'own' | 'rolled_up')}; unattributable observations are omitted."""
    starts, parents = {}, {}  # (harness, session, turn) -> min ts; (harness, session) -> parent session of a subagent thread
    for r in records:
        if r['thread_kind'] == 'subagent':
            if r.get('parent_session'):parents.setdefault((r['harness'], r['session']), r['parent_session'])
        elif r.get('turn_id'):
            key = (r['harness'], r['session'], r['turn_id'])
            starts[key] = min(starts.get(key, r['ts']), r['ts'], key=_t)
    turns = {}  # (harness, session) -> [(start, turn)] sorted by start
    for (harness, session, turn), start in starts.items():
        turns.setdefault((harness, session), []).append((_t(start), turn))
    for found in turns.values():found.sort()
    result = {}
    for r in records:
        if r['thread_kind'] != 'subagent':
            if r.get('turn_id'):result[r['id']] = (r['harness'], r['session'], r['turn_id'], 'own')
            continue
        session, seen = r['session'], {r['session']}
        for _ in range(MAX_DEPTH):
            parent = parents.get((r['harness'], session))
            if parent is None or parent == session:break  # Claude subagents share their parent's session
            if parent in seen:session = None;break
            seen.add(parent);session = parent
        found = turns.get((r['harness'], session)) if session else None
        at = bisect_right(found, (_t(r['ts']), chr(0x10FFFF))) - 1 if found else -1
        if at >= 0:result[r['id']] = (r['harness'], session, found[at][1], 'rolled_up')
        elif r.get('turn_id'):result[r['id']] = (r['harness'], r['session'], r['turn_id'], 'own_subagent')
    return result


def top_prompts(records, table, k=5, by='cost'):
    """Rank prompts by list-price cost (unpriced last) or total tokens; see module docstring for the roll-up."""
    if by not in ('cost', 'tokens'):raise ValueError("by must be 'cost' or 'tokens'")
    assigned = assign_prompts(records)
    groups = {}
    for r in records:
        if r['id'] in assigned:groups.setdefault(assigned[r['id']][:3], []).append(r)
    prompts = []
    for (harness, session, turn), rows in groups.items():
        own = [r for r in rows if r['thread_kind'] != 'subagent']
        subs = [r for r in rows if r['thread_kind'] == 'subagent']
        tokens = {c: sum((r['tokens'].get(c) or 0) for r in rows) for c in CLASSES}
        costs = [price_observation(r, table)['cost'] for r in rows]
        priced = [c for c in costs if c is not None]
        first, last = min((r['ts'] for r in rows), key=_t), max((r['ts'] for r in rows), key=_t)
        head = min(own or rows, key=lambda r: _t(r['ts']))
        prompts.append({
            'harness': harness, 'session': session, 'turn_id': turn, 'thread_kind': head['thread_kind'], 'machine': head.get('machine'),
            'project_id': head.get('project_id'), 'project_label': head.get('project_label'),
            'first_ts': first, 'last_ts': last, 'duration_s': (_t(last) - _t(first)).total_seconds(),
            'models': sorted({r['model'] for r in rows if r.get('model')}), 'requests': len(rows),
            'subagent_requests': len(subs), 'subagents': len({(r['session'], r.get('agent')) for r in subs}),
            'tokens': tokens, 'total_tokens': sum(tokens[c] for c in CLASSES if c != 'reasoning'),
            'cost': sum(priced) if priced else None, 'cost_complete': len(priced) == len(costs),
            'resume': RESUME[harness].format(session) if harness in RESUME else None})
    if by == 'cost':
        prompts.sort(key=lambda p: (p['cost'] is None, -(p['cost'] or 0), -p['total_tokens']))
    else:
        prompts.sort(key=lambda p: -p['total_tokens'])
    return {'prompts': prompts[:k], 'total_prompts': len(prompts),
            'unattributed_observations': len(records) - len(assigned), 'by': by}
