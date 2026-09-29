import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from usage import sessions
from usage.__main__ import main
from usage.history import History

BASE = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)
RATE = {'m-a': 1e-6, 'm-b': 2e-6}


def fake_price(obs):
    """Per-token fake: m-unk is unpriced (None), everything else RATE per counted token."""
    t = obs['tokens']
    total = sum(t.get(k) or 0 for k in ('fresh_input', 'cache_read', 'cache_write', 'output'))
    if obs['model'] not in RATE:
        return {'cost': None, 'currency': None, 'status': 'unpriced', 'parts': {}, 'assumptions': [],
                'reason': 'no price', 'price_ref': None}
    return {'cost': total * RATE[obs['model']], 'currency': 'USD', 'status': 'priced', 'parts': {},
            'assumptions': [], 'reason': None, 'price_ref': None}


def rec(harness, session, minute, model='m-a', *, agent='main', kind='main', parent=None, origin='cli',
        cwd='/w/app', src=None, fresh=100, out=50, warnings=(), complete=True):
    ts = (BASE + timedelta(minutes=minute)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'harness': harness, 'provider': 'p', 'model': model, 'effort': None, 'session': session,
            'parent_session': parent, 'thread_kind': kind, 'agent': agent, 'origin': origin, 'cwd': cwd,
            'project_id': cwd, 'turn_id': None, 'ts': ts, 'complete': complete, 'warnings': list(warnings),
            'tokens': {'fresh_input': fresh, 'cache_read': 0, 'cache_write': 0, 'output': out, 'reasoning': 0},
            'sources': [src] if src else []}


def sub(session, agent, minute, wf=None, **kw):
    path = f'/p/{session}/subagents/' + (f'workflows/{wf}/' if wf else '') + f'agent-{agent}.jsonl'
    return rec('claude', session, minute, agent=agent, kind='subagent', parent=session, src=path, **kw)


def claude_root():
    return [rec('claude', 'S', 0), rec('claude', 'S', 30), sub('S', 'a1', 2), sub('S', 'a2', 3, model='m-b'),
            sub('S', 'w1', 4, wf='wf_1'), sub('S', 'w2', 5, wf='wf_1')]


def find(node, pred):
    if pred(node):
        return node
    for child in node['children']:
        hit = find(child, pred)
        if hit:
            return hit


class TreeTests(unittest.TestCase):
    def test_claude_path_nesting_and_workflow(self):
        tree = sessions.build_tree(claude_root(), 'S', price=fake_price)['root']
        self.assertEqual((tree['kind'], tree['link']), ('main', 'root'))
        kinds = sorted((c['kind'], c['link']) for c in tree['children'])
        self.assertEqual(kinds, [('subagent', 'path'), ('subagent', 'path'), ('workflow', 'path')])
        wf = find(tree, lambda n: n['kind'] == 'workflow')
        self.assertEqual(wf['label'], 'workflow wf_1')
        self.assertEqual(sorted(c['id'] for c in wf['children']), ['w1', 'w2'])
        self.assertEqual(tree['own']['observations'], 2)
        self.assertEqual(tree['observations'], 6)

    def test_codex_parent_child(self):
        rows = [rec('codex', 'P', 0), rec('codex', 'C', 2, parent='P', kind='subagent', agent='worker'),
                rec('codex', 'G', 3, parent='C', kind='subagent')]
        tree = sessions.build_tree(rows, 'P', price=fake_price)['root']
        child = tree['children'][0]
        self.assertEqual((child['id'], child['link'], child['kind']), ('C', 'explicit', 'child'))
        self.assertEqual(child['children'][0]['id'], 'G')

    def test_inferred_inside_window_and_cwd_only(self):
        rows = claude_root() + [
            rec('codex', 'E1', 6, origin='codex_exec', cwd='/w/app/sub'), rec('codex', 'E1', 8, origin='codex_exec', cwd='/w/app/sub'),
            rec('codex', 'E2', 45, origin='codex_exec'), rec('codex', 'E2', 46, origin='codex_exec'),
            rec('codex', 'E3', 6, origin='codex_exec', cwd='/other')]
        result = sessions.build_tree(rows, 'S', price=fake_price)
        hit = find(result['root'], lambda n: n['id'] == 'E1')
        self.assertEqual(hit['link'], 'inferred')
        for gone in ('E2', 'E3'):
            self.assertIsNone(find(result['root'], lambda n: n['id'] == gone))
        self.assertEqual(result['unassigned'], [])
        off = sessions.build_tree(rows, 'S', infer=False, price=fake_price)
        self.assertIsNone(find(off['root'], lambda n: n['id'] == 'E1'))

    def test_ambiguous_candidate_unassigned(self):
        rows = claude_root() + [rec('claude', 'T', 5), rec('claude', 'T', 35),
                                rec('codex', 'E', 10, origin='codex_exec'), rec('codex', 'E', 12, origin='codex_exec')]
        result = sessions.build_tree(rows, 'S', price=fake_price)
        self.assertIsNone(find(result['root'], lambda n: n['id'] == 'E'))
        [item] = result['unassigned']
        self.assertEqual(item['id'], 'E')
        self.assertIn('ambiguous', item['reason'])

    def test_guardian_without_parent_unassigned(self):
        rows = claude_root() + [rec('codex', 'g1', 5, kind='subagent', agent='guardian'),
                                rec('codex', 'g1', 6, kind='subagent', agent='guardian')]
        result = sessions.build_tree(rows, 'S', price=fake_price)
        [item] = result['unassigned']
        self.assertEqual((item['harness'], item['id']), ('codex', 'g1'))
        self.assertIn('no parent', item['reason'])

    def test_root_resolution(self):
        rows = [rec('claude', 'X', 0), rec('codex', 'X', 1)]
        with self.assertRaises(ValueError) as cm:
            sessions.build_tree(rows, 'X', price=fake_price)
        self.assertIn('claude:X', str(cm.exception)); self.assertIn('codex:X', str(cm.exception))
        self.assertEqual(sessions.build_tree(rows, 'codex:X', price=fake_price)['root']['harness'], 'codex')
        with self.assertRaises(ValueError):
            sessions.build_tree(rows, 'nope', price=fake_price)

    def test_model_totals_equal_node_sums_and_coordination(self):
        rows = claude_root()
        result = sessions.build_tree(rows, 'S', price=fake_price)
        nodes = []
        def walk(n):
            nodes.append(n); [walk(c) for c in n['children']]
        walk(result['root'])
        for model, total in result['models'].items():
            self.assertEqual(total['observations'], sum(n['own']['by_model'].get(model, {}).get('observations', 0) for n in nodes))
            self.assertEqual(total['total'], sum(n['own']['by_model'].get(model, {}).get('total', 0) for n in nodes))
        self.assertEqual(result['total']['observations'], 6)
        self.assertEqual(result['total']['total'], 6 * 150)
        self.assertEqual(result['coordination']['total'], 300)
        self.assertAlmostEqual(result['models']['m-b']['cost']['USD'], 150 * 2e-6)

    def test_unknown_cost_is_na_and_coverage_below_one(self):
        rows = claude_root() + [sub('S', 'a3', 6, model='m-unk')]
        result = sessions.build_tree(rows, 'S', price=fake_price)
        self.assertLess(result['models']['m-unk']['cost_coverage'], 1)
        self.assertIsNone(result['models']['m-unk']['cost'])
        self.assertLess(result['total']['cost_coverage'], 1)
        text = sessions.render(result, None, 'DATE')
        line = [l for l in text.splitlines() if 'a3' in l][0]
        self.assertIn('n/a', line)
        self.assertNotIn('$0', line)
        self.assertIn('Costs are API-equivalent at list price (table retrieved DATE), not what was paid.', text)

    def test_lower_bound_propagates(self):
        rows = claude_root()
        rows[4] = sub('S', 'w1', 4, wf='wf_1', warnings=['output_not_final'])
        tree = sessions.build_tree(rows, 'S', price=fake_price)['root']
        wf = find(tree, lambda n: n['kind'] == 'workflow')
        self.assertTrue(find(wf, lambda n: n['id'] == 'w1')['lower_bound'])
        self.assertTrue(wf['lower_bound']); self.assertTrue(tree['lower_bound'])
        self.assertFalse(find(tree, lambda n: n['id'] == 'a1')['lower_bound'])
        self.assertIn('≥', sessions.render(sessions.build_tree(rows, 'S', price=fake_price), None, 'D'))
        rows2 = claude_root(); rows2[2] = sub('S', 'a1', 2, complete=False)
        self.assertTrue(sessions.build_tree(rows2, 'S', price=fake_price)['root']['lower_bound'])


def outcome_rows():
    return [rec('claude', 'S', 0), sub('S', 'a1', 1, fresh=600000, out=400000),
            sub('S', 'a2', 2, fresh=500000, out=0), sub('S', 'a3', 3, model='m-b', fresh=250000, out=0),
            sub('S', 'a4', 4, model='m-b', fresh=100000, out=0), sub('S', 'a5', 5)]


def line(unit, threads, outcome, root='S', v=1, **extra):
    d = {'v': v, 'root_session': root, 'unit': unit, 'threads': threads, 'outcome': outcome, 'note': '',
         'ts': '2026-09-10T10:00:00Z'}
    d.update(extra)
    return json.dumps(d)


def ag(i):
    return {'harness': 'claude', 'agent_id': i}


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'outcomes.jsonl'

    def test_load_v0_v1_malformed_and_other_roots(self):
        self.path.write_text('\n'.join([
            line('u1', [ag('a1')], 'pass'),
            line('u2', [ag('a2')], 'redo', v=0, kind='x', executor='y', usage={'z': 1}),
            line('u3', [ag('a3')], None, v=0),
            'not json', json.dumps({'v': 1, 'root_session': 'S'}),
            line('u9', [ag('a1')], 'pass', root='OTHER')]) + '\n')
        out = sessions.load_outcomes(self.path, 'S')
        self.assertEqual([o['unit'] for o in out], ['u1', 'u2'])
        self.assertEqual(out.malformed, 2)
        self.assertEqual(out.skipped, 1)
        self.assertEqual(sessions.load_outcomes(Path(self.tmp.name) / 'missing', 'S'), [])

    def test_cost_per_pass_hand_computed(self):
        self.path.write_text('\n'.join([
            line('u1', [ag('a1'), ag('a4')], 'pass'), line('u2', [ag('a2')], 'redo'),
            line('u3', [ag('a3')], 'wrong')]) + '\n')
        result = sessions.build_tree(outcome_rows(), 'S', price=fake_price)
        eff = sessions.efficiency(result, sessions.load_outcomes(self.path, 'S'))
        a, b = eff['models']['m-a'], eff['models']['m-b']
        self.assertEqual((a['total'], b['total']), (1500000, 350000))
        self.assertEqual((a['pass_units'], b['pass_units']), (1, 1))
        self.assertAlmostEqual(a['cost_per_pass']['USD'], 1.5); self.assertAlmostEqual(b['cost_per_pass']['USD'], 0.7)
        self.assertEqual(a['tokens_per_pass'], 1500000)
        self.assertEqual(a['units_by_outcome'], {'pass': 1, 'redo': 1})
        self.assertEqual(b['units_by_outcome'], {'pass': 1, 'wrong': 1})
        self.assertEqual(eff['unrated'], ['claude:agent:a5'])
        text = sessions.render(result, eff, 'D')
        self.assertIn('unrated', text); self.assertIn('coordination', text.lower())

    def test_zero_passes_is_na(self):
        self.path.write_text(line('u2', [ag('a2')], 'redo') + '\n')
        result = sessions.build_tree(outcome_rows(), 'S', price=fake_price)
        eff = sessions.efficiency(result, sessions.load_outcomes(self.path, 'S'))
        self.assertIsNone(eff['models']['m-a']['cost_per_pass'])
        self.assertIsNone(eff['models']['m-a']['tokens_per_pass'])
        self.assertIn('n/a', sessions.render(result, eff, 'D'))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state/history.sqlite3'
        proj = self.root / 'logs/proj'
        self.write(proj / 'S.jsonl', 'S', None, 'req0', '2026-09-10T10:00:00Z')
        self.write(proj / 'S/subagents/agent-a1.jsonl', 'S', 'a1', 'req1', '2026-09-10T10:05:00Z')
        with History(self.db) as h:
            h.refresh('claude', self.root / 'logs')
        patcher = patch('usage.sessions.default_pricer', return_value=(fake_price, '2026-09-01'))
        patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def write(path, session, agent, request, ts):
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'type': 'assistant', 'uuid': 'r' + request, 'requestId': request, 'sessionId': session,
               'cwd': '/w/app', 'version': 'v', 'timestamp': ts,
               'message': {'id': 'm' + request, 'model': 'm-a', 'stop_reason': 'end_turn',
                           'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20,
                                     'cache_creation_input_tokens': 0, 'output_tokens': 5}}}
        if agent:
            row['agentId'] = agent
        path.write_text(json.dumps(row) + '\n')

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['--db', str(self.db), *argv])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_session_text_and_json(self):
        text = self.run_main('session', 'S')
        self.assertIn('subagent a1', text); self.assertIn('table retrieved 2026-09-01', text)
        data = json.loads(self.run_main('session', 'S', '--json'))
        self.assertEqual(data['total']['observations'], 2)
        self.assertEqual(data['root']['children'][0]['link'], 'path')

    def test_rate_lists_appends_and_rejects(self):
        listing = self.run_main('rate', 'S')
        self.assertIn('claude:agent:a1', listing)
        outcomes = self.db.with_name('outcomes.jsonl')
        self.assertFalse(outcomes.exists())
        printed = self.run_main('rate', 'S', '--unit', 'u1', '--thread', 'claude:agent:a1', '--outcome', 'pass', '--note', 'ok')
        saved = json.loads(outcomes.read_text())
        self.assertEqual(json.loads(printed), saved)
        self.assertEqual((saved['v'], saved['root_session'], saved['outcome'], saved['note']), (1, 'S', 'pass', 'ok'))
        self.assertEqual(saved['threads'], [{'harness': 'claude', 'agent_id': 'a1'}])
        self.assertEqual(stat.S_IMODE(outcomes.stat().st_mode), 0o600)
        self.assertIn('pass', self.run_main('session', 'S'))
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            main(['--db', str(self.db), 'rate', 'S', '--unit', 'u2', '--thread', 'claude:agent:zzz', '--outcome', 'pass'])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(len(outcomes.read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
