import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenatlas import __main__ as cli, prompt_store, why
from tokenatlas.history import History
from tokenatlas.report import build_report, render_report, report_state
from test_fresh_report import Base, payload
from test_pricing import TABLE
from test_report import decode_html, expand, page_text
from test_top_prompts import claude_row, jl, ob, user

SECRET = 'secret u'  # the synthetic prompt text in test_top_prompts.user() is 'secret <uuid>'


EXTRA = dict(machine='m1', sources=[], complete=True, id_synthetic=False, warnings=[], effort=None, origin=None)


def rows(*specs, machine='m1'):
    """Prompts t<i>: one observation each, costlier for larger fresh count; sources name a fake log."""
    out = []
    for i, fresh in specs:
        r = ob(f'o{i}', f'{i:02d}', turn=f't{i}', fresh=fresh)
        r.update(machine=machine, sources=[f'/logs/{i}.jsonl'], complete=True, id_synthetic=False, warnings=[], effort=None, origin=None)
        out.append(r)
    return out


def fake(calls):
    def extract(harness, source, session, turn_id, limit=200):
        calls.append(turn_id)
        return f'TEXT-{turn_id}' if turn_id != 'tnone' else None
    return extract


class StoreUnit(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'top-prompts.json'
        self.calls = []

    def update(self, records, k=2, machine='m1', extract=None):
        return prompt_store.update(self.path, records, TABLE, machine, k=k, extract=extract or fake(self.calls))

    def test_store_path(self):
        self.assertEqual(prompt_store.store_path(Path('/x/y/history.sqlite3')), Path('/x/y/top-prompts.json'))

    def test_missing_file_is_empty(self):
        self.assertEqual(prompt_store.load(self.path), {})

    def test_update_writes_private_atomic_top_k(self):
        res = self.update(rows((1, 1000000), (2, 3000000), (3, 2000000)))
        self.assertEqual((res['kept'], res['added'], res['evicted'], res['path']), (0, 2, 0, str(self.path)))
        self.assertEqual(prompt_store.load(self.path),
                         {('claude', 's', 't2'): 'TEXT-t2', ('claude', 's', 't3'): 'TEXT-t3'})
        data = json.loads(self.path.read_text())
        self.assertEqual((data['version'], data['k'], data['by']), (1, 2, 'cost'))
        self.assertEqual(sorted(data['entries'][0]), ['captured_at', 'harness', 'session', 'text', 'turn_id'])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_eviction_removes_text_from_file_bytes(self):
        self.update(rows((1, 1000000), (2, 3000000)))
        self.assertIn(b'TEXT-t1', self.path.read_bytes())
        res = self.update(rows((1, 1000000), (2, 3000000), (3, 5000000), (4, 4000000)))
        self.assertEqual((res['kept'], res['added'], res['evicted']), (0, 2, 2))
        self.assertNotIn(b'TEXT-t1', self.path.read_bytes())
        self.assertNotIn(b'TEXT-t2', self.path.read_bytes())
        res = self.update(rows((1, 1000000), (2, 3000000), (3, 5000000), (4, 4000000), (5, 4500000)))
        self.assertEqual((res['kept'], res['added'], res['evicted']), (1, 1, 1))
        self.assertNotIn(b'TEXT-t4', self.path.read_bytes())

    def test_remote_machine_gets_no_text(self):
        self.update(rows((1, 1000000), (2, 3000000), machine='m2'))
        self.assertEqual(self.calls, [])
        self.assertEqual(prompt_store.load(self.path), {})

    def test_none_entry_is_retried_on_each_update(self):
        recs = rows((1, 3000000))
        recs[0]['turn_id'] = 'tnone'
        self.update(recs)
        self.assertEqual(prompt_store.load(self.path), {('claude', 's', 'tnone'): None})
        self.assertEqual(self.calls, ['tnone'])
        self.update(recs)
        self.assertEqual(self.calls, ['tnone', 'tnone'])
        self.update(recs, extract=lambda h, src, s, t, limit=200: 'now readable')
        self.assertEqual(prompt_store.load(self.path), {('claude', 's', 'tnone'): 'now readable'})
        self.update(recs, extract=lambda *a, **k: self.fail('a stored text is not re-extracted'))

    def test_only_the_prompts_own_sources_are_tried(self):
        recs = rows((1, 3000000))
        sub = ob('sub', '05', kind='subagent', parent='s', agent='x', fresh=1)
        sub.update(machine='m1', sources=['/logs/sub.jsonl'])
        seen = []
        self.update(recs + [sub], extract=lambda h, src, s, t, limit=200: seen.append(str(src)))
        self.assertEqual(seen, ['/logs/1.jsonl'])

    def test_written_only_on_change(self):
        recs = rows((1, 1000000), (2, 3000000))
        self.update(recs)
        before = self.path.stat()
        with patch.object(prompt_store.os, 'replace', wraps=os.replace) as replace:
            res = self.update(recs)
            self.assertEqual((res['kept'], res['added'], res['evicted']), (2, 0, 0))
            replace.assert_not_called()
            self.update(recs, k=1)
            replace.assert_called_once()
        self.assertGreater(self.path.stat().st_mtime_ns, before.st_mtime_ns - 1)

    def test_corrupt_file_warns_and_loads_empty(self):
        for bad in ('{not json', '[]', '{"version":1,"entries":[{"harness":1}]}'):
            self.path.write_text(bad)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(prompt_store.load(self.path), {})
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1, bad)
            self.assertIn('top-prompts.json', err.getvalue())

    def test_only_the_current_top_k_texts_are_shown(self):
        self.update(rows((1, 1000000), (2, 3000000)))
        recs = rows((1, 1000000), (2, 3000000), (3, 5000000))
        self.assertEqual(set(prompt_store.load(self.path)), {('claude', 's', 't1'), ('claude', 's', 't2')})
        shown = prompt_store.visible(self.path, recs, TABLE)  # recorded k=2: t3 and t2 are the top; t1 fell out
        self.assertEqual(shown, {('claude', 's', 't2'): 'TEXT-t2'})
        self.assertEqual(prompt_store.visible(self.path.with_name('none.json'), recs, TABLE), {})

    def write_good(self, mode=0o600):
        self.update(rows((1, 1000000)))
        os.chmod(self.path, mode)

    def warns(self, path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = prompt_store.load(path)
        return got, err.getvalue()

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_load_refuses_unsafe_files_with_one_line_warning(self):
        self.write_good()
        self.assertEqual(self.warns(self.path)[0], {('claude', 's', 't1'): 'TEXT-t1'})
        os.chmod(self.path, 0o644)
        got, err = self.warns(self.path)
        self.assertEqual((got, len(err.strip().splitlines())), ({}, 1))
        os.chmod(self.path, 0o600)
        link = self.path.with_name('link.json')
        os.link(self.path, link)
        got, err = self.warns(self.path)
        self.assertEqual((got, len(err.strip().splitlines())), ({}, 1))
        link.unlink()
        self.assertNotEqual(self.warns(self.path)[0], {})
        with patch.object(prompt_store.os, 'getuid', return_value=os.getuid() + 1):
            got, err = self.warns(self.path)
        self.assertEqual((got, len(err.strip().splitlines())), ({}, 1))
        target = self.path.with_name('real.json')
        os.rename(self.path, target)
        os.symlink(target, self.path)
        got, err = self.warns(self.path)
        self.assertEqual((got, len(err.strip().splitlines())), ({}, 1))
        self.path.unlink()
        self.path.mkdir()
        got, err = self.warns(self.path)
        self.assertEqual((got, len(err.strip().splitlines())), ({}, 1))

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_update_rewrites_an_unsafe_file_even_when_unchanged(self):
        recs = rows((1, 1000000))
        self.update(recs)
        os.chmod(self.path, 0o644)
        self.update(recs)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(prompt_store.load(self.path), {('claude', 's', 't1'): 'TEXT-t1'})

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_update_replaces_a_symlink_and_leaves_its_target(self):
        target = self.path.with_name('elsewhere.json')
        target.write_text('precious')
        os.symlink(target, self.path)
        self.update(rows((1, 1000000)))
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'precious')
        self.assertEqual(prompt_store.load(self.path), {('claude', 's', 't1'): 'TEXT-t1'})

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_update_replaces_a_hard_linked_file(self):
        recs = rows((1, 1000000))
        self.update(recs)
        other = self.path.with_name('other.json')
        os.link(self.path, other)
        self.update(recs)
        self.assertEqual(self.path.stat().st_nlink, 1)
        self.assertIn(b'TEXT-t1', other.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_update_cleans_only_stale_own_temp_files(self):
        import time
        old, fresh, other = (self.path.with_name(n) for n in ('.top-prompts-old', '.top-prompts-new', 'keep.txt'))
        for f in (old, fresh, other):f.write_text('x')
        two_hours = time.time() - 7200
        os.utime(old, (two_hours, two_hours))
        os.utime(other, (two_hours, two_hours))
        self.update(rows((1, 1000000)))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['.top-prompts-new', 'keep.txt', 'top-prompts.json'])

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_update_and_forget_clean_stale_temps_even_when_nothing_is_written(self):
        import time
        recs = rows((1, 1000000))
        self.update(recs)
        old = self.path.with_name('.top-prompts-old')
        for action in (lambda: self.update(recs), lambda: prompt_store.forget(self.path)):
            old.write_text('x')
            os.utime(old, (time.time() - 7200,) * 2)
            before = self.path.read_bytes()
            action()
            self.assertFalse(old.exists())
            if self.path.exists():self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == 'nt', 'POSIX file safety')
    def test_forget_unlinks_a_symlink_not_its_target_and_warns_on_hard_links(self):
        target = self.path.with_name('elsewhere.json')
        target.write_text('precious')
        os.symlink(target, self.path)
        prompt_store.forget(self.path)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'precious')
        self.write_good()
        other = self.path.with_name('other.json')
        os.link(self.path, other)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            prompt_store.forget(self.path)
        self.assertFalse(self.path.exists())
        self.assertIn('other hard links still hold the text', err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_forget(self):
        self.update(rows((1, 1000000)))
        prompt_store.forget(self.path)
        self.assertFalse(self.path.exists())
        prompt_store.forget(self.path)  # idempotent


class Cli(Base):
    def setUp(self):
        super().setUp()
        d = why.CLAUDE_PROJECTS / 'proj'
        jl(d / 'sess.jsonl', [user('2026-09-03T09:59:00Z', 'u1'), claude_row('2026-09-03T10:00:00Z', 'r1', 10),
                              user('2026-09-03T11:00:00Z', 'u2'), claude_row('2026-09-03T11:00:05Z', 'r2', 1000000),
                              user('2026-09-03T12:00:00Z', 'u3'), claude_row('2026-09-03T12:00:05Z', 'r3', 2000000)])
        self.prices = Path(self.tmp.name) / 'prices.json'
        self.prices.write_text(json.dumps(TABLE))
        self.store = self.db.parent / 'top-prompts.json'
        self.html = Path(self.tmp.name) / 'r.html'
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)

    def top(self, *args):
        return self.run_cli('top', '--prices', str(self.prices), *args)

    def keep(self, n='2'):
        code, out, err = self.top('--keep-text', '-n', n)
        self.assertEqual(code, 0, err)
        return out

    def test_no_code_path_creates_store_without_keep_text(self):
        with patch.object(cli, '_open_in_browser') as opened:
            for args in (('refresh', '--harness', 'claude'), ('refresh', '--all'), ('report', '--html', str(self.html)),
                         ('report', '--html', str(self.html), '--private'), ('open', '--html', str(self.html), '--no-refresh'),
                         ('open', '--html', str(self.html), '--shared'), ('top',), ('top', '--json', '--with-text'),
                         ('top', '--forget-text'), ('snapshot', str(Path(self.tmp.name) / 'snap.sqlite3')), ('doctor',)):
                self.run_cli(*args)
                self.assertFalse(self.store.exists(), args)
        opened.assert_called()

    def test_keep_text_table_json_and_forget(self):
        out = self.keep()
        self.assertEqual(out.count('\n    secret u'), 2, out)
        self.assertIn('secret u3', out)
        self.assertNotIn('secret u1', out)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode) if os.name != 'nt' else 0o600, 0o600)
        plain = json.loads(self.top('--json')[1])
        self.assertTrue(all('text' not in p for p in plain['prompts']))
        self.assertNotIn(SECRET, self.top('--json')[1])
        withtext = json.loads(self.top('--json', '--with-text')[1])
        self.assertEqual([p['text'] for p in withtext['prompts']], ['secret u3', 'secret u2', None][:len(withtext['prompts'])])
        code, out, _ = self.top('--forget-text')
        self.assertEqual((code, json.loads(out)), (0, {'forgotten': str(self.store)}))
        self.assertFalse(self.store.exists())
        self.assertNotIn(SECRET, self.top()[1])
        self.assertTrue(all(p['text'] is None for p in json.loads(self.top('--json', '--with-text')[1])['prompts']))

    def test_keep_text_needs_a_database(self):
        for p in self.db.parent.glob('history.sqlite3*'):
            p.unlink()
        self.assertEqual(self.top('--keep-text')[0], 2)
        self.assertFalse(self.store.exists())

    def test_snapshot_never_contains_stored_text(self):
        self.keep('3')
        out = Path(self.tmp.name) / 'snap.sqlite3'
        self.assertEqual(self.run_cli('snapshot', str(out))[0], 0)
        self.assertNotIn(b'secret u', out.read_bytes())
        self.assertNotIn(b'secret u', self.db.read_bytes())

    def test_reports_shared_never_private_with_text(self):
        self.keep('3')
        self.assertEqual(self.run_cli('report', '--html', str(self.html))[0], 0)
        shared = self.html.read_text()
        self.assertNotIn(SECRET, shared)
        self.assertNotIn(SECRET, json.dumps(payload(shared)))
        self.assertNotIn('prompt_texts', payload(shared))
        self.assertEqual(self.run_cli('report', '--html', str(self.html), '--private')[0], 0)
        self.assertEqual(sorted(payload(self.html.read_text())['prompt_texts'].values()),
                         ['secret u1', 'secret u2', 'secret u3'])
        with patch.object(cli, '_open_in_browser'):
            self.run_cli('open', '--html', str(self.html), '--no-refresh', '--shared')
            self.assertNotIn(SECRET, self.html.read_text())
            self.run_cli('open', '--html', str(self.html), '--no-refresh')
            self.assertIn('prompt_texts', payload(self.html.read_text()))

    def test_private_reports_embed_only_the_current_top_k(self):
        self.keep('3')
        d = why.CLAUDE_PROJECTS / 'proj'
        jl(d / 'sess2.jsonl', [user('2026-09-03T13:00:00Z', 'u4'), claude_row('2026-09-03T13:00:05Z', 'r4', 5000000)])
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        # the store still holds u1..u3 (recorded k=3), but u1 is no longer in the global top 3 {u4, u3, u2}
        self.assertEqual(len(prompt_store.load(self.store)), 3)
        self.assertEqual(self.run_cli('report', '--html', str(self.html), '--private')[0], 0)
        self.assertEqual(sorted(payload(self.html.read_text())['prompt_texts'].values()), ['secret u2', 'secret u3'])
        self.assertNotIn('secret u1', self.html.read_text())
        # a filtered report still ranks over the full history
        self.assertEqual(self.run_cli('report', '--html', str(self.html), '--private', '--start', '2026-09-03T12:00:00+00:00')[0], 0)
        self.assertEqual(sorted(payload(self.html.read_text())['prompt_texts'].values()), ['secret u3'])
        with patch.object(cli, '_open_in_browser'):
            self.run_cli('open', '--html', str(self.html), '--no-refresh')
        self.assertEqual(sorted(payload(self.html.read_text())['prompt_texts'].values()), ['secret u2', 'secret u3'])
        self.assertNotIn('secret u1', self.html.read_text())

    def test_private_conditional_report_rebuilds_when_store_changes_shared_does_not(self):
        self.keep('1')
        private = ('report', '--html', str(self.html), '--private', '--if-changed')
        shared = ('report', '--html', str(self.html), '--if-changed')
        self.run_cli(*private)
        self.assertTrue(json.loads(self.run_cli(*private)[1])['skipped'])
        self.keep('2')
        res = json.loads(self.run_cli(*private)[1])
        self.assertNotIn('skipped', res)
        self.assertTrue(json.loads(self.run_cli(*private)[1])['skipped'])
        self.top('--forget-text')
        self.assertNotIn('skipped', json.loads(self.run_cli(*private)[1]))
        self.run_cli(*shared)
        self.assertTrue(json.loads(self.run_cli(*shared)[1])['skipped'])
        self.keep('3')
        self.assertTrue(json.loads(self.run_cli(*shared)[1])['skipped'])

    def test_filtered_top_never_prints_text_outside_the_global_top_k(self):
        self.keep('3')
        jl(why.CLAUDE_PROJECTS / 'proj' / 'sess2.jsonl', [user('2026-09-03T13:00:00Z', 'u4'), claude_row('2026-09-03T13:00:05Z', 'r4', 5000000)])
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        self.assertEqual(len(prompt_store.load(self.store)), 3)  # u1 is stored but no longer in the global top 3
        window = ('--end', '2026-09-03T10:30:00+00:00')
        self.assertNotIn('secret u1', self.top(*window)[1])
        res = json.loads(self.top('--json', '--with-text', *window)[1])
        self.assertEqual([(p['turn_id'], p['text']) for p in res['prompts']], [('u1', None)])
        res = json.loads(self.top('--json', '--with-text', '--start', '2026-09-03T11:00:00+00:00')[1])
        self.assertEqual({p['turn_id']: p['text'] for p in res['prompts']}, {'u2': 'secret u2', 'u3': 'secret u3', 'u4': None})

    def test_young_private_report_rebuilds_when_visible_previews_change_shared_stays_throttled(self):
        self.keep('1')
        private = ('report', '--html', str(self.html), '--private', '--if-changed', '--max-age', '1h')
        shared = ('report', '--html', str(self.html), '--if-changed', '--max-age', '1h')
        self.run_cli(*private)
        self.assertEqual(json.loads(self.run_cli(*private)[1])['reason'], 'unchanged')
        self.keep('2')
        self.assertNotIn('skipped', json.loads(self.run_cli(*private)[1]))
        self.assertEqual(sorted(payload(self.html.read_text())['prompt_texts'].values()), ['secret u2', 'secret u3'])
        self.run_cli(*shared)
        self.keep('3')
        self.assertTrue(json.loads(self.run_cli(*shared)[1])['skipped'])


class ReportBuild(unittest.TestCase):
    def test_redacted_with_texts_raises(self):
        with self.assertRaises(ValueError):
            build_report(rows((1, 1)), {}, redact=True, prompt_texts={('claude', 's', 't1'): 'x'})
        with self.assertRaises(ValueError):
            build_report(rows((1, 1)), {}, redact=True, prompt_texts={})

    def test_prompt_ordinals_price_classes_and_no_cost_column(self):
        recs = rows((1, 1000000), (10, 3000000))
        recs.append(dict(ob('sub', '05', kind='subagent', parent='s', agent='x', fresh=1000000), **EXTRA))
        for redact in (True, False):
            report = build_report(recs, {}, redact=redact, table=TABLE)
            cols = report['columns']
            self.assertNotIn('cost', cols)
            self.assertNotIn('prompt', cols['dict'])
            self.assertNotIn('prompt', cols['idx'])
            self.assertEqual(cols['prompt'], [0, 0, 1])  # by first appearance in row order; no prompt-id strings
            self.assertTrue(all(isinstance(x, int) for x in cols['prompt']))
            self.assertNotIn('s:t1', json.dumps(cols))
            self.assertEqual((cols['price'], cols['cw1h']), ([0, 0, 0], [0, 0, 0]))
            self.assertEqual(len(cols['price_classes']), 1)
            self.assertEqual(len(cols['price_classes'][0]), 5)
            self.assertEqual([r['cost'] for r in expand(report)], [4.0, 4.0, 12.0])
            self.assertNotIn('prompt_texts', report)

    def test_unpriced_observation_has_null_price_class(self):
        recs = rows((1, 1000000))
        recs.append(dict(ob('u', '07', fresh=1000000, model='mystery-model'), **EXTRA))
        cols = build_report(recs, {}, redact=False, table=TABLE)['columns']
        self.assertEqual(cols['price'], [0, None])

    def test_private_texts_keyed_by_ordinal(self):
        recs = rows((1, 1000000), (2, 3000000))
        report = build_report(recs, {}, redact=False, prompt_texts={('claude', 's', 't2'): 'hello', ('claude', 's', 't1'): None,
                                                                    ('claude', 's', 'gone'): 'x'}, table=TABLE)
        got = {r['id']: r['prompt'] for r in expand(report)}
        self.assertEqual(got, {'o1': 0, 'o2': 1})
        self.assertEqual(report['prompt_texts'], {1: 'hello'})

    def test_unassigned_observation_has_no_prompt(self):
        recs = rows((1, 1))
        recs.append(dict(ob('n', '09', fresh=1), **EXTRA))
        report = build_report(recs, {}, redact=False, table=TABLE)
        self.assertEqual({r['id']: r['prompt'] for r in expand(report)}['n'], None)
        self.assertEqual(report['columns']['prompt'], [0, None])

    def test_state_includes_store_hash_only_when_given(self):
        a = report_state(1, 'm', {}, {}, 'tok')
        self.assertEqual(a, report_state(1, 'm', {}, {}, 'tok', texts_hash=None))
        b = report_state(1, 'm', {}, {}, 'tok', texts_hash='abc')
        self.assertNotEqual(a, b)

    def test_texts_hash_is_identity_not_data(self):
        a = report_state(1, 'm', {}, {}, 'tok')
        b = report_state(1, 'm', {}, {}, 'tok', texts_hash='abc')
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(a[1], b[1])

    def test_texts_hash(self):
        self.assertIsNone(prompt_store.texts_hash({}))
        one = prompt_store.texts_hash({('c', 's', 't'): 'a'})
        self.assertNotEqual(one, prompt_store.texts_hash({('c', 's', 't'): 'b'}))
        self.assertEqual(one, prompt_store.texts_hash({('c', 's', 't'): 'a'}))

    def test_template_card_uses_textcontent(self):
        html = page_text(render_report(build_report(rows((1, 1)), {}, redact=False, table=TABLE)))
        self.assertIn('Dyraste prompterna', html)
        self.assertIn('id="top-prompts"', html)


if __name__ == '__main__':
    unittest.main()
