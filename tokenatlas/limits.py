"""Limit hits: the turn that hit the 5-hour or weekly limit, and what tokenatlas saw filling that window.

A hit is a fact the logs state (a rejected Claude request, or a Codex observation whose rate_limits name a reached limit). What filled the
window is only what these logs contain: usage on claude.ai, ChatGPT or other machines counts toward the same limit but is not seen. Costs are
list prices in USD; a request without a complete USD price is counted as unpriced, never guessed."""
from datetime import datetime, timedelta

from tokenatlas import prompts

PROVIDER = {'claude': 'anthropic', 'codex': 'openai'}
TOP = 3
RETRY_GAP = timedelta(minutes=10)  # rejections without a reset time belong to one hit only within this gap
# The only limit types a shared report or the cost facts may name; any other string is neutral ("other").
PUBLIC_REACHED = frozenset(('five_hour', 'seven_day', 'rate_limit_reached', 'workspace_owner_credits_depleted', 'workspace_member_credits_depleted',
                            'workspace_owner_usage_limit_reached', 'workspace_member_usage_limit_reached'))


def public_reached(reached):
    return reached if reached in PUBLIC_REACHED or reached is None else 'other'


def _iso(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    except ValueError:
        return None


def _window(quota):
    """The window that was hit: the one with the highest used_percent (the first on a tie), or None."""
    windows = [w for w in (quota or {}).get('windows') or [] if isinstance(w, dict)]
    return max(windows, key=lambda w: w.get('used_percent') or 0) if windows else None


def _scope(row):
    """The hit's origin row metadata, for scoped reports only (never exported): project, session, turn, model, effort, agent and provider."""
    return dict(scope={k: row.get(k) for k in ('project_id', 'project_label', 'session', 'turn_id', 'model', 'effort', 'agent', 'provider')})


def _hit_window(quota):
    """The window a Codex hit names: the only window at 100 % or more, else None (an unknown type with no single full window names none)."""
    full = [w for w in (quota or {}).get('windows') or [] if isinstance(w, dict) and (w.get('used_percent') or 0) >= 100]
    return full[0] if len(full) == 1 else None


def label(minutes, reached=None):
    """('five_hour' | 'weekly' | raw text, minutes): a window is named by its length, never by its slot."""
    return 'five_hour' if minutes == 300 else 'weekly' if minutes == 10080 else (reached or None)


def limit_hits(records, events, table):
    """Hits, oldest first: {harness, at, reached, window_minutes, resets_at, retries, turn, window}. `turn` is (harness, session, turn_id) of
    the request running at the hit, or None. `window` (None when the window length is unknown) is {start, end, requests, priced_requests,
    unpriced_requests, cost, top: [{turn, requests, cost, share}]}: the same provider's list-price cost in [resets_at - minutes, at] and
    the three costliest turns in it as a share of that cost (unattributed requests count in the cost, not in the ranking)."""
    records, events = sorted(records, key=lambda r: r['ts']), sorted(events, key=lambda r: r['ts'])
    found = []
    claude, recent = {}, {}  # by reset time; and, for events without one, the latest hit per (harness, type) with its last event time
    for event in events:
        quota = event.get('quota') or {}
        window = _window(quota)
        resets = (window.get('resets_at') if window else None) or quota.get('resets_at')
        at = prompts._t(event['ts'])
        key = (event['harness'], quota.get('reached'), resets)
        if resets is None:
            known = recent.get(key[:2])
            if known and at - known[1] <= RETRY_GAP:
                known[0]['retries'] += 1
                recent[key[:2]] = (known[0], at)
                continue
        elif key in claude:
            claude[key]['retries'] += 1
            continue
        claude[key] = hit = dict(harness=event['harness'], at=event['ts'], reached=quota.get('reached'), window_minutes=window and window.get('minutes'),
                                 resets_at=resets, retries=1, _row=event, **_scope(event))
        if resets is None:
            recent[key[:2]] = (hit, at)
        found.append(hit)
    open_ = {}  # (harness, limit id) -> the open episode: reached type, window minutes, last reached time, sessions that reported it
    for record in records:
        quota = record.get('quota') or {}
        reached = quota.get('reached')
        if quota.get('status') == 'rejected' or not quota:
            continue  # a record without quota says nothing about a limit and leaves the consecutive state alone
        # Depleted credits are not a full time window: no window, no ranking (the raw type names it).
        window = None if reached and 'credits' in reached else _hit_window(quota)
        account = (record['harness'], quota.get('limit_id'))  # consecutive per harness and limit, never across them
        at, minutes = prompts._t(record['ts']), window and window.get('minutes')
        episode = open_.get(account)
        # An episode is one hit. It ends on recovery reported by a session that itself reported the reached state in it (a stale null from another
        # session proves nothing), on a gap longer than its window, or when a different window is named. A reset time only slides, so its
        # movement alone never starts a new hit; overlapping per-session episodes are one hit, at the earliest observation.
        if episode and at - episode['last'] > timedelta(minutes=episode['minutes'] or 300):
            episode = open_[account] = None
        if not reached:
            if episode and record['session'] in episode['sessions']:
                episode = open_[account] = None
            continue
        if episode and episode['reached'] == reached and (not minutes or not episode['minutes'] or minutes == episode['minutes']):
            episode['sessions'].add(record['session'])
            episode['last'] = at
            episode['minutes'] = episode['minutes'] or minutes
            continue
        found.append(dict(harness=record['harness'], at=record['ts'], reached=reached, window_minutes=minutes or None,
                          resets_at=window.get('resets_at') if window else None, retries=1, rolling=True, _row=record, **_scope(record)))
        open_[account] = dict(reached=reached, minutes=minutes or None, last=at, sessions={record['session']})
    if not found:
        return []  # the usual case: skip the turn assignment over the whole history
    found.sort(key=lambda h: h['at'])
    # One assignment, over usage only (as top_prompts and the report do); a rejection carries its own turn and never feeds the assignment.
    assigned = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}
    threads = {}  # subagent thread -> [(time, assigned turn)] of its usage, for a rejection in a subagent file (it carries no usable turn of its own)
    for r in records:
        if r['thread_kind'] == 'subagent' and assigned[id(r)]:
            threads.setdefault(prompts._thread(r), []).append((prompts._t(r['ts']), tuple(assigned[id(r)][:3])))
    for found_turns in threads.values():
        found_turns.sort(key=lambda x: x[0])
    for hit in found:
        row = hit.pop('_row')
        turn = assigned.get(id(row))
        hit['turn'] = tuple(turn[:3]) if turn else (row['harness'], row['session'], row['turn_id']) if row.get('turn_id') else None
        bound = threads.get(prompts._thread(row)) if not turn and row.get('thread_kind') == 'subagent' else None
        if bound:  # the latest usage in the thread at or before the rejection, else the earliest after it
            at = prompts._t(row['ts'])
            before = [x for x in bound if x[0] <= at]
            hit['turn'] = (before[-1] if before else bound[0])[1]
        hit['window'] = _fill(hit, records, assigned, table)
    return found


def _fill(hit, records, assigned, table):
    """What was seen in the window before a hit. Claude's 5-hour limit is a session block and its weekly limit resets at a fixed time, so the
    window is [resets_at - minutes, hit]. Codex windows are rolling ("rolling window duration" in its protocol) and the reset time moves,
    so the window is [hit - minutes, hit]."""
    resets, minutes, at = _iso(hit['resets_at']), hit['window_minutes'], _iso(hit['at'])
    rolling = hit.get('rolling')
    if (resets is None and not rolling) or not minutes or at is None:
        return None
    start = at - timedelta(minutes=minutes) if rolling else resets - timedelta(minutes=minutes)
    aliases = table.get('provider_aliases') or {}
    canon = lambda p: aliases.get(p, p)
    provider = canon(PROVIDER.get(hit['harness']))
    turns, requests, priced, total, lower = {}, 0, 0, 0.0, False
    for record in records:
        if record['harness'] != hit['harness'] or canon(record.get('provider')) != provider or record.get('id_synthetic') or not start <= prompts._t(record['ts']) <= at:
            continue  # an ambiguous identity is in no total, as in the cost facts
        requests += 1
        lower = lower or not record.get('complete', True)
        cost = prompts._cost(record, table)
        found = assigned.get(id(record))
        entry = turns.setdefault(tuple(found[:3]), [0.0, 0, 0, record['ts']]) if found else [0.0, 0, 0, record['ts']]
        entry[1] += 1
        entry[3] = min(entry[3], record['ts'], key=prompts._t)
        if cost is not None:
            priced += 1
            total += cost
            entry[0] += cost
            entry[2] += 1
    ranked = sorted(((k, v) for k, v in turns.items() if v[2]), key=lambda kv: (-kv[1][0], kv[0]))[:TOP]
    return dict(start=start.isoformat(), end=at.isoformat(), requests=requests, priced_requests=priced, unpriced_requests=requests - priced,
                cost=total if priced else None, lower_bound=lower,
                top=[dict(turn=k, requests=v[1], cost=v[0], share=v[0] / total if total > 0 else None, first_ts=v[3]) for k, v in ranked])


def hit_turns(hits):
    """{(harness, session, turn_id): hit}: the first hit of each turn, for marking turn cards."""
    out = {}
    for hit in hits:
        if hit['turn'] and hit['turn'] not in out:
            out[hit['turn']] = hit
    return out


def limit_hit(hit):
    return dict(reached=hit['reached'], window_minutes=hit['window_minutes'], at=hit['at'])


def mark_turns(items, hits):
    """Add `limit_hit` to each ranked turn (dict with harness, session, turn_id) that hit a limit; others are left untouched."""
    turns = hit_turns(hits)
    for item in items:
        hit = turns.get((item['harness'], item['session'], item['turn_id']))
        if hit:
            item['limit_hit'] = limit_hit(hit)
    return items


def badge(hit):
    """English words for a turn card: names the window by its length."""
    name = label(hit['window_minutes'], hit['reached'])
    return {'five_hour': 'Hit the 5-hour limit', 'weekly': 'Hit the weekly limit'}.get(name) or f'Hit a limit ({name})' if name else 'Hit a limit'


def scope_hits(hits, records, harness=None, start=None, end=None, project=None, session=None, turn=None, model=None, effort=None, provider=None, agent=None, universe=None):
    """Hits that belong to a filtered report: the hit's harness and time must match the harness and start/end filters. With a project, session,
    turn, model, effort, provider or agent filter a hit is kept when its turn is among the filtered `records`' turns or when the hit's own row
    (a rejection has no usage record) matches every given filter; `universe` is the whole history's usage, the basis of the hits' turns. The hits themselves are computed over the whole history first."""
    given = {'project_id': project, 'turn_id': turn, 'model': model, 'effort': effort, 'provider': provider, 'agent': agent}
    given = {k: v for k, v in given.items() if v is not None}
    prefix = None
    if isinstance(session, str):
        head, separator, rest = session.partition(':')
        if separator and head in ('claude', 'codex', 'pi', 'opencode'):
            prefix, session = head, rest
    if prefix is not None and harness is not None and prefix != harness:
        return []  # as History.records: a session prefix that contradicts the harness filter selects nothing
    harness = harness or prefix
    if session is not None:
        given['session'] = session
    turns = None
    if given:
        # Hits carry rolled-up turn identities, so map the filtered records through the same whole-history assignment.
        everything = universe if universe is not None else records
        found = {prompts.ident(r): a for r, a in zip(everything, prompts.assign_prompts(everything))}
        turns = {tuple(found[prompts.ident(r)][:3]) if found.get(prompts.ident(r)) else (r['harness'], r['session'], r.get('turn_id')) for r in records}
    out = []
    for hit in hits:
        at = prompts._t(hit['at'])
        if not ((harness is None or hit['harness'] == harness) and (start is None or at >= start) and (end is None or at < end)):
            continue
        if turns is not None and hit['turn'] not in turns and any((hit.get('scope') or {}).get(k) != v for k, v in given.items()):
            continue
        out.append(hit)
    return out
