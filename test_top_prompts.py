import dataclasses
import json
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why
from tokenatlas.history import COLLECTOR_VERSION, History, merge_observations
from tokenatlas.prompts import assign_prompts, top_prompts
from test_fresh_report import Base
from test_pricing import TABLE, CLAUDE


def ob(i, ts, session='s', turn=None, kind='main', parent=None, agent='main', harness='claude', model='claude-x',
       provider='anthropic', fresh=0, read=0, write=0, out=0, reasoning=0, project='/w/app'):
    return {'id': i, 'harness': harness, 'session': session, 'agent': agent, 'thread_kind': kind,
            'parent_session': parent, 'turn_id': turn, 'turn_confidence': 'derived' if turn else 'absent',
            'ts': f'2026-09-03T10:{ts}:00+00:00', 'model': model, 'provider': provider, 'machine': 'm1',
            'project_id': project, 'project_label': Path(project).name, 'raw_usage': {}, 'tariff': None,
            'tokens': dict(fresh_input=fresh, cache_write=write, cache_read=read, output=out, reasoning=reasoning)}


class Assign(unittest.TestCase):
    def test_claude_same_session_subagent_goes_to_earlier_turn(self):
        rows = [ob('a', '00', turn='t1'), ob('sub', '05', kind='subagent', parent='s', agent='x'), ob('b', '10', turn='t2')]
        got = assign_prompts(rows)
        self.assertEqual(got['a'], ('claude', 's', 't1', 'own'))
        self.assertEqual(got['sub'], ('claude', 's', 't1', 'rolled_up'))
        self.assertEqual(got['b'], ('claude', 's', 't2', 'own'))

    def test_child_session_subagent_with_own_turn_rolls_up(self):
        for harness in ('codex', 'opencode'):
            rows = [ob('a', '00', session='p', turn='t1', harness=harness),
                    ob('c', '05', session='c', turn='ct', kind='subagent', parent='p', harness=harness),
                    ob('b', '10', session='p', turn='t2', harness=harness)]
            got = assign_prompts(rows)
            self.assertEqual(got['c'], (harness, 'p', 't1', 'rolled_up'))

    def test_nested_subagent(self):
        rows = [ob('a', '00', session='p', turn='t1', harness='codex'),
                ob('c', '01', session='c', turn='x', kind='subagent', parent='p', harness='codex'),
                ob('g', '02', session='g', turn='y', kind='subagent', parent='c', harness='codex')]
        self.assertEqual(assign_prompts(rows)['g'], ('codex', 'p', 't1', 'rolled_up'))

    def test_before_any_turn_unattributed(self):
        rows = [ob('sub', '00', kind='subagent', parent='s'), ob('a', '05', turn='t1'),
                ob('noturn', '06'), ob('orphan', '07', session='z', kind='subagent', parent='gone')]
        got = assign_prompts(rows)
        self.assertNotIn('sub', got)
        self.assertNotIn('noturn', got)
        self.assertNotIn('orphan', got)
        self.assertIn('a', got)

    def test_cycle_guard(self):
        rows = [ob('a', '00', session='p', turn='t1', harness='codex'),
                ob('x', '01', session='x', kind='subagent', parent='y', harness='codex'),
                ob('y', '01', session='y', kind='subagent', parent='x', harness='codex')]
        got = assign_prompts(rows)
        self.assertEqual(set(got), {'a'})

    def test_sessions_do_not_cross_harness(self):
        rows = [ob('a', '00', session='s', turn='t1', harness='codex'),
                ob('sub', '05', session='s', kind='subagent', parent='s', harness='claude')]
        self.assertNotIn('sub', assign_prompts(rows))


class Rank(unittest.TestCase):
    def test_cost_ranking_with_unpriced_and_na(self):
        rows = [ob('a', '00', turn='t1', fresh=1000000),
                ob('a2', '01', turn='t1', model='mystery', fresh=5),
                ob('b', '10', turn='t2', fresh=3000000),
                ob('c', '20', session='u', turn='t3', harness='pi', provider='zz', model='nope', out=9000000)]
        res = top_prompts(rows, TABLE, k=5)
        self.assertEqual([p['turn_id'] for p in res['prompts']], ['t2', 't1', 't3'])
        t2, t1, t3 = res['prompts']
        self.assertAlmostEqual(t2['cost'], 12.0)
        self.assertTrue(t2['cost_complete'])
        self.assertAlmostEqual(t1['cost'], 4.0)
        self.assertFalse(t1['cost_complete'])
        self.assertEqual(t1['requests'], 2)
        self.assertIsNone(t3['cost'])
        self.assertFalse(t3['cost_complete'])
        self.assertEqual(t1['resume'], 'claude --resume s')
        self.assertIsNone(t3['resume'])
        self.assertEqual((res['total_prompts'], res['unattributed_observations'], res['by']), (3, 0, 'cost'))
        self.assertEqual(len(top_prompts(rows, TABLE, k=1)['prompts']), 1)

    def test_tokens_ranking_and_fields(self):
        rows = [ob('a', '00', turn='t1', fresh=1, write=2, read=3, out=10, reasoning=4),
                ob('sub', '05', kind='subagent', parent='s', agent='ag', model='claude-y', fresh=100, out=1, reasoning=1),
                ob('sub2', '06', kind='subagent', parent='s', agent='ag', fresh=1),
                ob('b', '30', turn='t2', fresh=50)]
        res = top_prompts(rows, TABLE, by='tokens')
        first = res['prompts'][0]
        self.assertEqual(first['turn_id'], 't1')
        self.assertEqual(first['total_tokens'], 1 + 2 + 3 + 10 + 100 + 1 + 1)
        self.assertEqual(first['tokens'], dict(fresh_input=102, cache_write=2, cache_read=3, output=11, reasoning=5))
        self.assertEqual((first['requests'], first['subagent_requests'], first['subagents']), (3, 2, 1))
        self.assertEqual(first['models'], ['claude-x', 'claude-y'])
        self.assertEqual((first['first_ts'], first['last_ts'], first['duration_s']),
                         ('2026-09-03T10:00:00+00:00', '2026-09-03T10:06:00+00:00', 360.0))
        self.assertEqual(first['project_label'], 'app')
        self.assertEqual(res['by'], 'tokens')

    def test_codex_resume(self):
        res = top_prompts([ob('a', '00', session='cx', turn='t', harness='codex', provider='openai', model='gpt-x', fresh=1)], TABLE)
        self.assertEqual(res['prompts'][0]['resume'], 'codex resume cx')


def jl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


def claude_row(ts, req, out, agent=None, session='sess'):
    row = {'type': 'assistant', 'uuid': 'r-' + req, 'requestId': req, 'sessionId': session, 'cwd': '/work/app',
           'timestamp': ts, 'message': {'id': 'm-' + req, 'model': 'claude-x', 'stop_reason': 'end_turn',
           'usage': {'input_tokens': 1000000, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': out}}}
    if agent:
        row['agentId'] = agent
    return row


def user(ts, uuid):
    return {'type': 'user', 'uuid': uuid, 'timestamp': ts, 'message': {'role': 'user', 'content': 'secret ' + uuid}}


class Cli(Base):
    def setUp(self):
        super().setUp()
        d = why.CLAUDE_PROJECTS / 'proj'
        jl(d / 'sess.jsonl', [user('2026-09-03T09:59:00Z', 'u1'), claude_row('2026-09-03T10:00:00Z', 'r1', 10),
                              user('2026-09-03T11:00:00Z', 'u2'), claude_row('2026-09-03T11:00:05Z', 'r2', 1000000)])
        jl(d / 'sess/subagents/agent-a.jsonl', [claude_row('2026-09-03T10:05:00Z', 'r3', 1000000, agent='a')])
        self.prices = Path(self.tmp.name) / 'prices.json'
        self.prices.write_text(json.dumps(TABLE))
        # provider for claude rows is 'anthropic' in the synthetic table through the harness default
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)

    def top(self, *args):
        return self.run_cli('top', '--prices', str(self.prices), *args)

    def test_json(self):
        code, out, err = self.top('--json')
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual([p['turn_id'] for p in res['prompts']], ['u1', 'u2'])
        self.assertEqual(res['total_prompts'], 2)
        u1 = res['prompts'][0]
        self.assertEqual((u1['requests'], u1['subagent_requests'], u1['subagents']), (2, 1, 1))
        self.assertEqual(u1['resume'], 'claude --resume sess')
        self.assertEqual(self.top('--json', '--by', 'tokens', '-n', '1')[1].count('"turn_id"'), 1)

    def test_table(self):
        code, out, err = self.top()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertIn('claude', lines[1])
        self.assertIn('app', lines[1])
        self.assertIn('claude --resume sess', lines[1])
        self.assertIn('$', lines[1])
        self.assertNotIn('secret', out)

    def test_table_na_and_lower_bound(self):
        self.prices.write_text(json.dumps(dict(TABLE, models=[])))
        self.assertIn('n/a', self.top()[1])
        only = dict(CLAUDE, model='other')
        self.prices.write_text(json.dumps(dict(TABLE, models=[only])))
        self.assertIn('n/a', self.top()[1])

    def test_filters(self):
        self.assertEqual(json.loads(self.top('--json', '--harness', 'codex')[1])['prompts'], [])
        self.assertEqual(json.loads(self.top('--json', '--project', '/nope')[1])['prompts'], [])
        res = json.loads(self.top('--json', '--start', '2026-09-03T10:30:00+00:00')[1])
        self.assertEqual([p['turn_id'] for p in res['prompts']], ['u2'])
        res = json.loads(self.top('--json', '--end', '2026-09-03T10:30:00+00:00')[1])
        self.assertEqual([p['turn_id'] for p in res['prompts']], ['u1'])
        self.assertEqual(json.loads(self.top('--json', '--project', '/work/app')[1])['total_prompts'], 2)

    def test_errors(self):
        self.assertEqual(self.top('--start', '2026-09-03T10:30:00')[0], 2)
        self.assertEqual(self.top('-n', '0')[0], 2)
        self.db.unlink() if self.db.exists() else None
        for p in self.db.parent.glob('history.sqlite3*'):
            p.unlink()
        self.assertEqual(self.top()[0], 2)


def pi_rows(extra_user=True):
    rows = [{'type': 'session', 'id': 'pis', 'timestamp': '2026-09-03T10:00:00Z', 'cwd': '/work/pi', 'version': 3},
            {'type': 'message', 'id': 'pre', 'timestamp': '2026-09-03T10:00:01Z',
             'message': {'role': 'assistant', 'provider': 'p', 'model': 'm', 'responseId': 'r0',
                         'usage': {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}}}]
    for n, (u, r) in enumerate((('u1', 'r1'), ('u2', 'r2'))):
        rows.append({'type': 'message', 'id': u, 'timestamp': f'2026-09-03T10:0{n + 1}:00Z',
                     'message': {'role': 'user', 'content': 'hello'}})
        rows.append({'type': 'message', 'id': 'a' + u, 'timestamp': f'2026-09-03T10:0{n + 1}:05Z',
                     'message': {'role': 'assistant', 'provider': 'p', 'model': 'm', 'responseId': r,
                                 'usage': {'input': 5, 'output': 5, 'cacheRead': 0, 'cacheWrite': 0}}})
    return rows


class PiAndFingerprint(Base):
    def test_pi_turn_ids_and_noop_refresh(self):
        jl(why.PI_SESSIONS / 'proj/s.jsonl', pi_rows())
        with History(self.db) as h:
            first = h.refresh('pi', why.PI_SESSIONS)
            got = {r['id']: (r['turn_id'], r['turn_confidence']) for r in h.records()}
            second = h.refresh('pi', why.PI_SESSIONS)
        self.assertEqual(first['files_parsed'], 1)
        self.assertEqual(got, {'r0': (None, 'absent'), 'r1': ('u1', 'derived'), 'r2': ('u2', 'derived')})
        self.assertEqual((second['files_parsed'], second['files_skipped']), (0, 1))

    def test_fingerprint_old_formula_for_other_harnesses(self):
        path = self.home / 'f.jsonl'
        path.write_text('x')
        s = path.stat()
        old = json.dumps([COLLECTOR_VERSION, ['f.jsonl', s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]])
        for harness in (None, 'claude', 'opencode'):
            self.assertEqual(History.fingerprint(path, harness=harness), old)
        for harness in ('pi', 'codex'):
            self.assertEqual(json.loads(History.fingerprint(path, harness=harness)), json.loads(old) + [['revision', 1]])

    def test_old_pi_fingerprint_forces_one_reread(self):
        jl(why.PI_SESSIONS / 'proj/s.jsonl', pi_rows())
        with History(self.db) as h:
            h.refresh('pi', why.PI_SESSIONS)
            p = why.PI_SESSIONS / 'proj/s.jsonl'
            s = p.stat()
            old = json.dumps([COLLECTOR_VERSION, ['s.jsonl', s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]])
            h.connection.execute('UPDATE files SET fingerprint=?', (old,))
            h.connection.commit()
            self.assertEqual(h.refresh('pi', why.PI_SESSIONS)['files_parsed'], 1)


TS = '2026-09-03T10:00:00Z'


def cx(row_type, payload, n=0):
    return {'timestamp': f'2026-09-03T10:{n:02d}:00Z', 'type': row_type, 'payload': payload}


def cx_msg(role, text, n=0, row_type='response_item'):
    return cx(row_type, {'type': 'message', 'role': role, 'id': f'{role}-{n}',
                         'content': [{'type': 'input_text' if role != 'assistant' else 'output_text', 'text': text}]}, n)


def cx_tokens(n):
    return {'timestamp': f'2026-09-03T10:{n:02d}:30Z', 'ordinal': n, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'last_token_usage': {'input_tokens': 10 * n, 'output_tokens': n},
        'total_token_usage': {'input_tokens': 10 * n, 'output_tokens': n, 'total_tokens': 11 * n}}}}


def cx_meta():
    return cx('session_meta', {'id': 'cs1', 'model_provider': 'openai', 'cwd': '/w'})


def current_rollout(second_user_id=None):
    ctx = lambda t, n: cx('turn_context', {'turn_id': t, 'model': 'gpt-x', 'cwd': '/w'}, n)
    done = lambda t, n: cx('event_msg', {'type': 'item_completed', 'turn_id': t}, n)
    return [cx_meta(),
            cx_msg('developer', 'rules', 1), cx_msg('user', '<environment_context><cwd>/w</cwd></environment_context>', 1),
            cx('world_state', {}, 1), ctx('T1', 2), cx_msg('user', 'real prompt one', 2), done('T1', 2),
            cx_msg('assistant', 'thinking', 3), cx('response_item', {'type': 'custom_tool_call'}, 3),
            cx('token_usage_record', {'turn_id': 'T1'}, 3), cx_tokens(3),
            cx_msg('user', 'steer please', 4), done('T1', 4), cx_msg('assistant', 'done', 5), cx_tokens(5),
            cx_msg('user', 'real prompt two', 6), ctx('T2', 7), cx_msg('assistant', 'ok', 8), cx_tokens(8)]


class CodexTurns(Base):
    def write(self, name, rows):
        jl(why.CODEX_SESSIONS / name, rows)
        return why.CODEX_SESSIONS / name

    def turns(self, rows):
        path = self.write('rollout-a.jsonl', rows)
        start, end = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 10, tzinfo=timezone.utc)
        return [(r.turn_id, r.turn_confidence) for r in why.collect_codex(why.CODEX_SESSIONS, start, end, paths=[path])]

    def test_current_format_attributes_to_explicit_turns(self):
        self.assertEqual(self.turns(current_rollout()),
                         [('T1', 'observed'), ('T1', 'observed'), ('T2', 'observed')])

    def test_legacy_only_genuine_user_events_start_turns(self):
        rows = [cx_meta(), cx_msg('developer', 'rules', 1), cx_msg('assistant', 'hello', 1),
                cx_msg('user', 'first', 2), cx('turn_context', {'model': 'm'}, 2), cx_tokens(2),
                cx_msg('assistant', 'reply', 3), cx_tokens(3),
                cx('event_msg', {'type': 'user_message', 'message': 'second', 'id': 'ev2'}, 4),
                cx('turn_context', {'model': 'm'}, 4), cx_tokens(4)]
        self.assertEqual(self.turns(rows), [('user-2', 'derived'), ('user-2', 'derived'), ('ev2', 'derived')])

    def test_merge_replaces_turn_fields_whichever_side_is_newer(self):
        self.write('rollout-a.jsonl', current_rollout())
        with History(self.db) as h:
            h.refresh('codex', why.CODEX_SESSIONS)
            stored = h.records()[0]
        bad = dict(stored, turn_id='assistant-9', turn_confidence='derived')
        for a, b in ((bad, stored), (stored, bad)):
            merged = merge_observations(a, b)
            self.assertEqual((merged['turn_id'], merged['turn_confidence']),
                             (stored['turn_id'], 'observed') if 'observed' in (a['turn_confidence'], b['turn_confidence'])
                             else (b['turn_id'], 'derived'))

    def test_refresh_after_revision_bump_stores_corrected_turns(self):
        self.write('rollout-a.jsonl', current_rollout())
        real = why.collect_codex

        def stale(*args, **kw):  # what the pre-fix parser produced: assistant message ids as derived turns
            return [dataclasses.replace(r, turn_id='assistant-old', turn_confidence='derived') for r in real(*args, **kw)]
        with History(self.db) as h, unittest.mock.patch.object(why, 'collect_codex', stale):
            h.refresh('codex', why.CODEX_SESSIONS)
            self.assertEqual({(r['turn_id'], r['turn_confidence']) for r in h.records()}, {('assistant-old', 'derived')})
        with History(self.db) as h:
            h.connection.execute("UPDATE files SET fingerprint='stale'")
            h.connection.commit()
            self.assertEqual(h.refresh('codex', why.CODEX_SESSIONS)['files_parsed'], 1)
            self.assertEqual(sorted((r['turn_id'], r['turn_confidence']) for r in h.records()),
                             [('T1', 'observed'), ('T1', 'observed'), ('T2', 'observed')])


class OrphanSubagents(unittest.TestCase):
    def test_orphan_subagent_with_own_turn_gets_own_prompt(self):
        rows = [ob('a', '00', session='p', turn='t1', harness='codex'),
                ob('o', '05', session='z', turn='zt', kind='subagent', parent='gone', harness='codex'),
                ob('o2', '06', session='z', turn='zt', kind='subagent', parent='gone', harness='codex', fresh=5)]
        got = assign_prompts(rows)
        self.assertEqual(got['o'], ('codex', 'z', 'zt', 'own_subagent'))
        res = top_prompts(rows, TABLE, k=5)
        sub = [p for p in res['prompts'] if p['turn_id'] == 'zt'][0]  # a group of only subagent rows must not crash
        self.assertEqual((sub['thread_kind'], sub['requests'], sub['session']), ('subagent', 2, 'z'))
        self.assertEqual([p['thread_kind'] for p in res['prompts'] if p['turn_id'] == 't1'], ['main'])


if __name__ == '__main__':
    unittest.main()
