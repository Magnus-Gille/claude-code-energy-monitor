"""Fixed context overhead: sizes and floors of what every call re-reads (system prompt, instruction files, skills).

Privacy: only character counts, component/skill names, counts, session ids, timestamps and token counts are
stored, never content.  Instruction-file paths are kept as basename plus a stable hash of the full path.
Tables live in the history database but are owned here; they are rebuilt per session by rescanning (rescan-all
with an upsert per harness+session, no per-file fingerprint), so a rescan of unchanged logs is idempotent.
"""
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

CHARS_PER_TOKEN = 4
HARNESSES = ('claude', 'codex', 'pi', 'opencode')
RESIDUAL_LABEL = 'other: tools, first prompt, unlogged'
COST_LABEL = 'API-equivalent at list price, estimate'
_DDL = (
    'CREATE TABLE IF NOT EXISTS overhead_sessions (harness TEXT NOT NULL, session TEXT NOT NULL, is_subagent INTEGER,'
    ' first_ts TEXT, floor_tokens INTEGER, floor_input INTEGER, floor_cache_write INTEGER, floor_cache_read INTEGER,'
    ' calls INTEGER, unidentified_skill_reads INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(harness, session))',
    'CREATE TABLE IF NOT EXISTS overhead_items (harness TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL,'
    ' name TEXT NOT NULL, chars INTEGER NOT NULL, source TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS overhead_skill_uses (harness TEXT NOT NULL, session TEXT NOT NULL, name TEXT NOT NULL,'
    ' chars INTEGER NOT NULL, ts TEXT, source TEXT NOT NULL)',
)
_SKILLS = re.compile(r'<skills_instructions>.*?</skills_instructions>', re.S)
_AGENTS = re.compile(r'<INSTRUCTIONS>.*?</INSTRUCTIONS>', re.S)
_ENV = re.compile(r'<environment_context>.*?</environment_context>', re.S)
_SKILL_PATH = re.compile(r'''(?:~|/)[^\s'"`]*?/skills/(?P<name>[A-Za-z0-9][A-Za-z0-9._:-]{0,80})/SKILL\.md''')
_GUTTER = re.compile(r'(?m)^[ \t]*\d+[\t\u2192]')
_FRONT_NAME = re.compile(r'(?m)^name:[ \t]*[\'"]?([A-Za-z0-9][A-Za-z0-9._:-]{0,80})[\'"]?[ \t]*$')
_HEADING = re.compile(r'#{1,6}[ \t]+\S')
_MIN_BODY = 200


def _skill_paths(command):
    """Names of concrete `.../skills/<name>/SKILL.md` paths in a command; [] when any mention is not one.

    A path inside a glob, brace expansion, variable or format pattern (`{ } * $` in its token) is rejected."""
    names = []
    for m in _SKILL_PATH.finditer(command):
        start = max(command.rfind(c, 0, m.start()) for c in ' \t\r\n\'"`') + 1
        ends = [i for i in (command.find(c, m.end()) for c in ' \t\r\n\'"`') if i >= 0]
        token = command[start:min(ends) if ends else len(command)]
        if any(c in token for c in '{}*$'):
            return []
        names.append(m.group('name'))
    return names if names and command.count('SKILL.md') == len(names) else []


def _chunks(output):
    """Text chunks of an exec output without its `Script ... Wall time ... Output:` header.

    A list output is the `text` of its input_text/output_text items; a leading item starting with `Script` and
    holding a `Wall time` line is the header.  A string is one chunk after the first line equal to `Output:`."""
    if isinstance(output, str):
        lines = output.split('\n')
        for i, line in enumerate(lines):
            if line.strip() == 'Output:':
                return ['\n'.join(lines[i + 1:])]
        return [output]
    if not isinstance(output, list):
        return []
    texts = [str(v.get('text') or '') for v in output
             if isinstance(v, dict) and v.get('type') in ('input_text', 'output_text')]
    if texts and texts[0].startswith('Script') and re.search(r'(?m)^Wall time\b', texts[0]):
        texts = texts[1:]
    return texts


def _skill_body(output):
    """(is_body, frontmatter name or None) for a chunk that should be a SKILL.md body."""
    if not isinstance(output, str) or len(output) < _MIN_BODY:
        return False, None
    text = output.lstrip()
    if _GUTTER.match(text):
        text = _GUTTER.sub('', text).lstrip()
    if text.startswith('---'):
        head = text[3:].split('\n---', 1)
        found = _FRONT_NAME.search(head[0]) if len(head) == 2 else None
        return (True, found.group(1)) if found else (False, None)
    return (True, None) if _HEADING.match(text) else (False, None)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text_len(value):
    """Characters of a string, or of the text parts of a content list; content is measured, never kept."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_len(v.get('text') if isinstance(v, dict) else v) for v in value)
    return 0 if value is None else len(json.dumps(value))


def _named(path):
    """Basename plus a stable hash of the full path; the directory itself is never stored."""
    return f'{Path(str(path)).name}#{hashlib.sha256(str(path).encode()).hexdigest()[:10]}'


def _rows(path):
    try:
        handle = open(path, encoding='utf-8', errors='replace')
    except OSError as exc:
        print(f'overhead: cannot read {path.name}: {exc}', file=sys.stderr)
        return
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _session(harness, session, sub=False):
    return {'harness': harness, 'session': session, 'is_subagent': sub, 'first_ts': None, 'floor_tokens': None,
            'floor_breakdown': None, 'calls': 0, 'components': [], 'skill_uses': [], 'unidentified_skill_reads': 0}


def _floor(row, inp, write, read):
    if row['floor_tokens'] is None and inp + write + read > 0:
        row['floor_tokens'] = inp + write + read
        row['floor_breakdown'] = {'input': inp, 'cache_write': write, 'cache_read': read}


def _item(row, kind, name, chars, source='exact'):
    row['components'].append({'kind': kind, 'name': name, 'chars': chars, 'source': source})


def _dict(value):
    return value if isinstance(value, dict) else {}


def _claude_attachment(row, a):
    kind = a.get('type')
    names = [str(n) for n in a.get('addedNames') or ()]
    if kind == 'skill_listing':
        _item(row, 'skill_listing', f"{_int(a.get('skillCount'))} skills", _text_len(a.get('content')))
    elif kind == 'instructions':
        for f in a.get('files') or ():
            if isinstance(f, dict):
                _item(row, 'instructions', _named(f.get('path')), _text_len(f.get('content')))
    elif kind == 'nested_memory':
        _item(row, 'nested_memory', _named(a.get('path')), _text_len(a.get('content')))
    elif kind == 'mcp_instructions_delta':
        _item(row, 'mcp_instructions', ','.join(names), _text_len(a.get('addedBlocks')))
    elif kind in ('deferred_tools_delta', 'deferred_tools_record'):
        _item(row, 'deferred_tools', ','.join(names), _text_len(a.get('addedLines')))
    elif kind == 'agent_listing_delta':
        _item(row, 'agent_listing', ','.join(str(n) for n in a.get('addedTypes') or ()), _text_len(a.get('addedLines')))
    elif kind == 'prompt_snapshot':
        _item(row, 'system_prompt', 'system_prompt', _text_len(a.get('systemPrompt')))
    elif kind == 'session_context':
        _item(row, 'session_context', 'session_context', _text_len(a.get('context')))


def _scan_claude(root):
    out = []
    for path in sorted(Path(root).rglob('*.jsonl')):
        sub = 'subagents' in path.parts
        parent = path.parent.parent.name if sub else None
        row = _session('claude', f'{parent}/{path.stem}' if sub else path.stem, sub)
        requests, uses, pending = set(), [], {}
        for r in _rows(path):
            row['first_ts'] = row['first_ts'] or r.get('timestamp')
            kind, message = r.get('type'), _dict(r.get('message'))
            if kind == 'attachment':
                _claude_attachment(row, _dict(r.get('attachment')))
            elif kind == 'assistant':
                usage = message.get('usage')
                if isinstance(usage, dict):
                    requests.add(r.get('requestId') or message.get('id') or f'row{len(requests)}')
                    _floor(row, _int(usage.get('input_tokens')), _int(usage.get('cache_creation_input_tokens')),
                           _int(usage.get('cache_read_input_tokens')))
                for block in message.get('content') if isinstance(message.get('content'), list) else ():
                    if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Skill':
                        pending[block.get('id')] = str(_dict(block.get('input')).get('skill') or 'unknown')
            elif kind == 'user' and r.get('isMeta') and r.get('sourceToolUseID') in pending:
                row['skill_uses'].append({'name': pending.pop(r['sourceToolUseID']), 'chars': _text_len(message.get('content')),
                                          'ts': r.get('timestamp'), 'source': 'exact'})
        row['calls'] = len(requests)
        if row['floor_tokens'] is not None or row['components'] or row['skill_uses']:
            out.append(row)
    return out


def _scan_codex(root):
    out = []
    for path in sorted(Path(root).rglob('rollout-*.jsonl')):
        row = _session('codex', path.stem)
        totals, calls, reads = [], {}, []
        for r in _rows(path):
            p = _dict(r.get('payload'))
            row['first_ts'] = row['first_ts'] or p.get('timestamp') or r.get('timestamp')
            kind = r.get('type')
            if kind == 'session_meta':
                row['session'] = str(p.get('id') or row['session'])
                row['is_subagent'] = isinstance(_dict(p.get('source')).get('subagent'), dict)
                _item(row, 'system_prompt', 'system_prompt', _text_len(_dict(p.get('base_instructions')).get('text')))
            elif kind == 'response_item' and p.get('type') == 'message':
                text = '\n'.join(str(_dict(c).get('text') or '') for c in p.get('content') or ())
                if p.get('role') == 'developer':
                    for m in _SKILLS.findall(text):
                        _item(row, 'skills_listing', 'skills_listing', len(m))
                elif p.get('role') == 'user':
                    for pattern, name in ((_AGENTS, 'agents_md'), (_ENV, 'environment_context')):
                        for m in pattern.findall(text):
                            _item(row, name, name, len(m))
            elif kind == 'response_item' and p.get('type') == 'custom_tool_call' and p.get('name') == 'exec':
                command = str(p.get('input') or '')
                if 'SKILL.md' in command:
                    calls[p.get('call_id')] = (_skill_paths(command), r.get('timestamp'))
            elif kind == 'response_item' and p.get('type') == 'custom_tool_call_output' and p.get('call_id') in calls:
                names, ts = calls.pop(p['call_id'])
                good = [(len(c), _skill_body(c)[1]) for c in _chunks(p.get('output')) if _skill_body(c)[0]]
                if not names or not good:
                    row['unidentified_skill_reads'] += 1
                    continue
                if len(names) == 1:
                    size, declared = next((g for g in good if g[1] == names[0]), good[0])
                    sized = [(declared or names[0], size)]
                else:  # several concrete paths: match chunks by frontmatter name, else split equally
                    by_name = {}
                    for size, declared in good:
                        by_name.setdefault(declared, size)
                    if all(n in by_name for n in names):
                        sized = [(n, by_name[n]) for n in names]
                    else:
                        sized = [(n, sum(g[0] for g in good) // len(names)) for n in names]
                for name, size in sized:
                    row['skill_uses'].append({'name': name, 'chars': size, 'ts': ts, 'source': 'heuristic'})
            elif kind == 'event_msg' and p.get('type') == 'token_count' and isinstance(p.get('info'), dict):
                info = p['info']
                last = _dict(info.get('last_token_usage'))
                total = _dict(info.get('total_token_usage')).get('total_tokens')
                if last and (total is None or total not in totals):
                    totals.append(total)
                    total_in, cached = _int(last.get('input_tokens')), _int(last.get('cached_input_tokens'))
                    if row['floor_tokens'] is None:
                        _floor(row, total_in - min(cached, total_in), 0, min(cached, total_in))
        row['calls'] = len(totals)
        if row['floor_tokens'] is not None or row['components'] or row['skill_uses'] or row['unidentified_skill_reads']:
            out.append(row)
    return out


def _scan_pi(root):
    out = []
    for path in sorted(Path(root).rglob('*.jsonl')):
        row, seen = _session('pi', path.stem), set()
        for r in _rows(path):
            if r.get('type') == 'session':
                row['session'] = str(r.get('id') or row['session'])
                row['first_ts'] = r.get('timestamp')
            usage = _dict(_dict(r.get('message')).get('usage'))
            if r.get('type') == 'message' and _dict(r.get('message')).get('role') == 'assistant' and usage:
                inp, write, read = _int(usage.get('input')), _int(usage.get('cacheWrite')), _int(usage.get('cacheRead'))
                if inp + write + read > 0:
                    row['first_ts'] = row['first_ts'] or r.get('timestamp')
                    seen.add(_dict(r.get('message')).get('responseId') or r.get('id') or f'row{len(seen)}')
                    _floor(row, inp, write, read)
        row['calls'] = len(seen)
        if row['floor_tokens'] is not None:
            out.append(row)
    return out


def _ms(value):
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(value) % 1000:03d}Z'
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _scan_opencode(root):
    try:
        con = sqlite3.connect(Path(root).expanduser().resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as exc:
        print(f'overhead: cannot open opencode database: {exc}', file=sys.stderr)
        return []
    rows = {}
    try:
        con.execute('PRAGMA query_only=ON')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        parents = dict(con.execute('SELECT id, parent_id FROM session')) if 'session' in tables else {}
        for sid, created, data in con.execute('SELECT session_id, time_created, data FROM message ORDER BY time_created, id'):
            try:
                d = json.loads(data)
            except (TypeError, ValueError):
                continue
            row = rows.setdefault(sid, _session('opencode', sid, parents.get(sid) is not None))
            row['first_ts'] = row['first_ts'] or _ms(created)
            tokens = _dict(_dict(d).get('tokens'))
            if _dict(d).get('role') == 'assistant' and tokens:
                cache = _dict(tokens.get('cache'))
                inp, read, write = _int(tokens.get('input')), _int(cache.get('read')), _int(cache.get('write'))
                if inp + read + write > 0:
                    row['calls'] += 1
                    _floor(row, inp, write, read)
        if 'part' in tables:
            for sid, created, data in con.execute('SELECT session_id, time_created, data FROM part ORDER BY time_created, id'):
                try:
                    d = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if _dict(d).get('type') == 'tool' and d.get('tool') == 'skill':
                    state = _dict(d.get('state'))
                    row = rows.setdefault(sid, _session('opencode', sid, parents.get(sid) is not None))
                    row['skill_uses'].append({'name': str(_dict(state.get('input')).get('name') or 'unknown'),
                                              'chars': _text_len(state.get('output')), 'ts': _ms(created), 'source': 'exact'})
    finally:
        con.close()
    return [r for r in rows.values() if r['floor_tokens'] is not None or r['skill_uses']]


def scan(harness, root):
    """Session dicts for one harness root.  `calls` counts distinct priced requests (Claude requestId, else
    message id; Codex distinct cumulative totals; Pi responseId/entry id; OpenCode assistant messages with tokens)."""
    return {'claude': _scan_claude, 'codex': _scan_codex, 'pi': _scan_pi, 'opencode': _scan_opencode}[harness](root)


def ensure(db):
    for sql in _DDL:
        db.execute(sql)
    if 'unidentified_skill_reads' not in {r[1] for r in db.execute('PRAGMA table_info(overhead_sessions)')}:
        db.execute('ALTER TABLE overhead_sessions ADD COLUMN unidentified_skill_reads INTEGER NOT NULL DEFAULT 0')


def save(db, harness, sessions):
    """Upsert sessions by harness+session; a session's items and skill uses are replaced wholesale."""
    if not db.in_transaction:
        db.execute('BEGIN')
    ensure(db)
    for s in sessions:
        key = (harness, s['session'])
        b = s['floor_breakdown'] or {}
        for table in ('overhead_items', 'overhead_skill_uses'):
            db.execute(f'DELETE FROM {table} WHERE harness=? AND session=?', key)
        db.execute('INSERT OR REPLACE INTO overhead_sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (*key, int(s['is_subagent']), s['first_ts'], s['floor_tokens'], b.get('input'), b.get('cache_write'),
                    b.get('cache_read'), s['calls'], s.get('unidentified_skill_reads', 0)))
        db.executemany('INSERT INTO overhead_items VALUES (?,?,?,?,?,?)',
                       [(*key, c['kind'], c['name'], c['chars'], c['source']) for c in s['components']])
        db.executemany('INSERT INTO overhead_skill_uses VALUES (?,?,?,?,?,?)',
                       [(*key, u['name'], u['chars'], u['ts'], u['source']) for u in s['skill_uses']])
    db.commit()


def _pct(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _moment(text):
    if not text:
        return None
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _dominant(records):
    counts = {}
    for r in records:
        key = (r.get('harness'), str(r.get('session')).split('/')[0])
        model = counts.setdefault(key, {})
        model[(r.get('provider'), r.get('model'))] = model.get((r.get('provider'), r.get('model')), 0) + 1
    return {k: max(sorted(v, key=str), key=v.get) for k, v in counts.items()}


def summarize(db, since=None, until=None, harness=None, records=None, prices=None):
    """Per-harness floor distribution, estimated component tokens, residual, skill uses and recurring cost.

    `records` (History.records) supplies each session's dominant model; without it the cost stays None.
    """
    ensure(db)
    lo, hi = _moment(since), _moment(until)
    sessions = {}
    for h, s, sub, ts, floor, inp, write, read, calls, unidentified in db.execute(
            'SELECT harness, session, is_subagent, first_ts, floor_tokens, floor_input, floor_cache_write,'
            ' floor_cache_read, calls, unidentified_skill_reads FROM overhead_sessions ORDER BY harness, session'):
        when = _moment(ts)
        if harness not in (None, h) or (lo and (when is None or when < lo)) or (hi and (when is None or when >= hi)):
            continue
        sessions[(h, s)] = {'floor': floor, 'calls': calls, 'kinds': {}, 'skills': [], 'sub': bool(sub),
                          'unidentified': unidentified}
    for h, s, kind, chars in db.execute('SELECT harness, session, kind, chars FROM overhead_items'):
        if (h, s) in sessions:
            sessions[(h, s)]['kinds'][kind] = sessions[(h, s)]['kinds'].get(kind, 0) + chars
    for h, s, name, chars, source in db.execute('SELECT harness, session, name, chars, source FROM overhead_skill_uses'):
        if (h, s) in sessions:
            sessions[(h, s)]['skills'].append((name, chars, source))
    table = None
    if records is not None:
        from usage import pricing
        table = prices if prices is not None else pricing.load_prices()
        models = _dominant(records)
    result = {'chars_per_token': CHARS_PER_TOKEN, 'token_basis': 'estimate', 'harnesses': {}}
    for h in sorted({k[0] for k in sessions}):
        mine = {k[1]: v for k, v in sessions.items() if k[0] == h}
        floors = [v['floor'] for v in mine.values() if v['floor'] is not None]
        kinds, skills, residual = {}, {}, []
        for v in mine.values():
            for kind, chars in v['kinds'].items():
                kinds.setdefault(kind, []).append(chars)
            for name, chars, source in v['skills']:
                skills.setdefault((name, source), []).append(chars)
            if v['kinds'] and v['floor'] is not None:
                residual.append(v['floor'] - sum(v['kinds'].values()) / CHARS_PER_TOKEN)
        cost, priced, unpriced = 0.0, 0, 0
        currency = None
        for s, v in mine.items():
            if v['floor'] is None:
                continue
            price = None
            model = models.get((h, s.split('/')[0])) if table is not None else None
            if model and v['calls'] > 1:
                price = pricing.price_observation({'harness': h, 'provider': model[0], 'model': model[1], 'tokens': {
                    'fresh_input': 0, 'cache_read': v['floor'], 'cache_write': 0, 'output': 0}}, table)['cost']
            elif model and v['calls'] <= 1:
                price = 0.0
            if price is None:
                unpriced += 1
            else:
                cost += price * max(v['calls'] - 1, 0)
                priced += 1
        if table is not None and priced:
            currency = next((m.get('currency') for m in table['models']), None)
        result['harnesses'][h] = {
            'sessions': len(mine), 'subagent_sessions': sum(v['sub'] for v in mine.values()),
            'floor': {'n': len(floors), 'median': statistics.median(floors) if floors else None,
                      'p10': _pct(floors, 0.1) if floors else None, 'p90': _pct(floors, 0.9) if floors else None},
            'components_available': bool(kinds),
            'components': {k: {'n': len(c), 'median_chars': statistics.median(c),
                               'estimated_tokens': statistics.median(c) / CHARS_PER_TOKEN, 'label': 'estimate'}
                           for k, c in sorted(kinds.items())},
            'residual': {'median_tokens': statistics.median(residual) if residual else None, 'label': RESIDUAL_LABEL},
            'unidentified_skill_reads': sum(v['unidentified'] for v in mine.values()),
            'skill_uses': [{'name': n, 'source': src, 'count': len(c), 'median_chars': statistics.median(c),
                            'estimated_tokens': statistics.median(c) / CHARS_PER_TOKEN, 'label': 'estimate'}
                           for (n, src), c in sorted(skills.items(), key=lambda i: (-len(i[1]), i[0]))],
            'recurring_cost': {'cost': cost if priced else None, 'currency': currency, 'sessions_priced': priced,
                               'sessions_unpriced': unpriced, 'label': COST_LABEL}}
    return result


def _n(value):
    return '-' if value is None else f'{value:,.0f}'


def render(result):
    lines = [f"Fixed context overhead; component tokens are estimates at {result['chars_per_token']} characters per token."]
    if not result['harnesses']:
        lines.append('No overhead data; run with --refresh.')
    for h, r in result['harnesses'].items():
        f = r['floor']
        lines += ['', f"{h}: {r['sessions']} sessions ({r['subagent_sessions']} subagent); floor tokens n={f['n']} "
                      f"median={_n(f['median'])} p10={_n(f['p10'])} p90={_n(f['p90'])}"]
        if not r['components_available']:
            lines.append('  components unavailable for this harness (floor only)')
        for kind, c in r['components'].items():
            lines.append(f"  {kind:<20} n={c['n']:<5} median {_n(c['median_chars']):>9} chars  ~{_n(c['estimated_tokens']):>8} tokens (estimate)")
        if r['residual']['median_tokens'] is not None:
            lines.append(f"  {r['residual']['label']}: median ~{_n(r['residual']['median_tokens'])} tokens (estimate)")
        rc = r['recurring_cost']
        lines.append(f"  recurring re-read cost: {'unknown' if rc['cost'] is None else format(rc['cost'], ',.2f') + ' ' + (rc['currency'] or '')}"
                     f" over {rc['sessions_priced']} priced sessions ({rc['sessions_unpriced']} unpriced); {rc['label']}")
        for u in r['skill_uses']:
            lines.append(f"  skill {u['name']:<24} uses={u['count']:<4} median {_n(u['median_chars']):>8} chars  ~{_n(u['estimated_tokens']):>7} tokens ({u['source']}, estimate)")
        if r['unidentified_skill_reads']:
            lines.append(f"  unidentified skill reads: {r['unidentified_skill_reads']} (heuristic; not a concrete skill path or body)")
    return '\n'.join(lines)


def run(args):
    """`overhead` command: optional rescan of the default roots, then the report; returns the exit status."""
    import why
    from usage.history import History
    if not args.refresh and not args.db.expanduser().is_file():
        raise ValueError('history database does not exist; run refresh or overhead --refresh first')
    roots = {'claude': why.CLAUDE_PROJECTS, 'codex': why.CODEX_SESSIONS, 'pi': why.PI_SESSIONS, 'opencode': why.OPENCODE_DB}
    with History(args.db) as history:
        db = history.connection
        if args.refresh:
            for h in (args.harness,) if args.harness else HARNESSES:
                if Path(roots[h]).exists():
                    db.execute('BEGIN')
                    save(db, h, scan(h, roots[h]))
        db.execute('BEGIN')
        result = summarize(db, since=args.since, harness=args.harness, records=history.records())
        db.rollback()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return 0
