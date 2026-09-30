import base64
import contextlib
import gzip
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenatlas import why, __main__ as cli
from tokenatlas.history import History
from tokenatlas import report as report_mod
from test_history import write_claude

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('demo_script', ROOT / 'scripts/demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def payload(html):
    packed = re.search(r'id="report-data"[^>]*>([^<]+)<', html).group(1)
    return json.loads(gzip.decompress(base64.b64decode(packed)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.db = self.state / 'tokenatlas/history.sqlite3'
        h = self.home
        for name, value in (('CLAUDE_PROJECTS', h / '.claude/projects'), ('CODEX_SESSIONS', h / '.codex/sessions'),
                            ('PI_SESSIONS', h / '.pi/agent/sessions'), ('OPENCODE_DB', h / '.local/share/opencode/opencode.db'),
                            ('COWORK_SESSIONS', h / 'cowork')):
            p = patch.object(why, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'HOME': str(h), 'USERPROFILE': str(h), 'XDG_STATE_HOME': str(self.state)})
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *args, db=True):
        out, err = io.StringIO(), io.StringIO()
        argv = (['--db', str(self.db)] if db == 'explicit' else []) + list(args)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def revision(self):
        with History(self.db) as h:
            return h.revision

    def claude(self, name='a.jsonl', **kw):
        path = why.CLAUDE_PROJECTS / 'proj' / name
        write_claude(path, **kw)
        return path


class RefreshAll(Base):
    def test_all_four_present(self):
        demo.build_home(self.home, 1)
        code, out, _ = self.run_cli('refresh', '--all')
        result = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([(x['harness'], x['status']) for x in result['harnesses']],
                         [(h, 'ok') for h in ('claude', 'codex', 'pi', 'opencode')])

    def test_absent_claude_and_cowork(self):
        self.claude()
        code, out, _ = self.run_cli('refresh', '--all')
        result = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(result['status'], 'ok')
        by = {x['harness']: x for x in result['harnesses']}
        self.assertEqual(by['claude']['status'], 'ok')
        for h in ('codex', 'pi', 'opencode'):
            self.assertEqual(by[h], {'harness': h, 'status': 'absent'})

    def test_no_roots_at_all_is_ok_and_absent(self):
        code, out, _ = self.run_cli('refresh', '--all')
        result = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual({x['status'] for x in result['harnesses']}, {'absent'})

    def test_cowork_only_counts_as_present(self):
        write_claude(why.COWORK_SESSIONS / 'o/a/local_1/.claude/projects/p/a.jsonl')
        code, out, _ = self.run_cli('refresh', '--all')
        by = {x['harness']: x for x in json.loads(out)['harnesses']}
        self.assertEqual(code, 0, out)
        self.assertEqual(by['claude']['status'], 'ok')
        self.assertEqual(by['claude']['observations_seen'], 1)

    def test_partial_harness_exits_2(self):
        path = self.claude()
        with path.open('a') as f:
            f.write('{not json\n')
        code, out, _ = self.run_cli('refresh', '--all')
        result = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'partial')

    def test_root_with_all_rejected_and_one_required(self):
        for args in (('refresh', '--all', '--root', str(self.home)), ('refresh', '--all', '--harness', 'pi'), ('refresh',)):
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args)[0], 2)


class RefreshErrors(Base):
    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'needs POSIX non-root')
    def test_stat_permission_error_is_not_absent(self):
        self.claude()
        parent = why.CODEX_SESSIONS.parent
        parent.mkdir(parents=True)
        parent.chmod(0)
        self.addCleanup(parent.chmod, 0o700)
        code, out, _ = self.run_cli('refresh', '--all')
        by = {x['harness']: x for x in json.loads(out)['harnesses']}
        self.assertNotEqual(by['codex']['status'], 'absent')
        self.assertEqual(code, 2, out)

    def test_oserror_in_one_harness_does_not_stop_others(self):
        demo.build_home(self.home, 1)
        real = History.refresh
        def refresh(hist, name, root, *a, **k):
            if name == 'codex':
                raise OSError('boom')
            return real(hist, name, root, *a, **k)
        with patch.object(History, 'refresh', refresh):
            code, out, _ = self.run_cli('refresh', '--all')
        result = json.loads(out)
        by = {x['harness']: x for x in result['harnesses']}
        self.assertEqual(by['codex']['status'], 'error')
        self.assertEqual(by['codex']['errors'], ['OSError: boom'])
        for h in ('claude', 'pi', 'opencode'):
            self.assertEqual(by[h]['status'], 'ok')
        self.assertNotEqual(result['status'], 'ok')
        self.assertEqual(code, 2)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'needs POSIX non-root')
    def test_unlistable_cowork_marks_claude_partial(self):
        self.claude()
        why.COWORK_SESSIONS.mkdir()
        why.COWORK_SESSIONS.chmod(0)
        self.addCleanup(why.COWORK_SESSIONS.chmod, 0o700)
        code, out, _ = self.run_cli('refresh', '--all')
        claude = {x['harness']: x for x in json.loads(out)['harnesses']}['claude']
        self.assertNotEqual(claude['status'], 'ok')
        self.assertTrue(claude['errors'])
        self.assertEqual(code, 2)


class Revision(Base):
    def test_first_refresh_increments_and_noop_does_not(self):
        self.claude()
        with History(self.db) as h:
            self.assertEqual(h.revision, 0)
            h.refresh('claude', why.CLAUDE_PROJECTS)
            first = h.revision
            self.assertGreater(first, 0)
            self.assertEqual(h.refresh('claude', why.CLAUDE_PROJECTS)['files_skipped'], 1)
            self.assertEqual(h.revision, first)
            self.assertEqual(h.doctor()['revision'], first)

    def test_appended_row_increments(self):
        path = self.claude()
        with History(self.db) as h:
            h.refresh('claude', why.CLAUDE_PROJECTS)
            before = h.revision
            extra = self.claude('b.jsonl', request='req2')
            h.refresh('claude', why.CLAUDE_PROJECTS)
            self.assertGreater(h.revision, before)
            before = h.revision
            rows = path.read_text().splitlines()
            row = json.loads(rows[-1]); row['requestId'] = 'req3'; row['message']['id'] = 'msg-req3'
            with path.open('a') as f:
                f.write(json.dumps(row) + '\n')
            h.refresh('claude', why.CLAUDE_PROJECTS)
            self.assertGreater(h.revision, before)

    def test_touched_file_updates_checkpoint_and_increments(self):
        path = self.claude()
        with History(self.db) as h:
            h.refresh('claude', why.CLAUDE_PROJECTS)
            os.utime(path, (1, 1))
            before = h.revision
            self.assertEqual(h.refresh('claude', why.CLAUDE_PROJECTS)['files_parsed'], 1)
            self.assertEqual(h.revision, before + 1)  # a checkpoint update counts as a change
            self.assertEqual(h.refresh('claude', why.CLAUDE_PROJECTS)['files_skipped'], 1)
            self.assertEqual(h.revision, before + 1)

    def test_import_increments_only_when_something_changed(self):
        src = Path(self.tmp.name) / 'src'
        write_claude(src / 'p/a.jsonl')
        snap = Path(self.tmp.name) / 'snap.sqlite3'
        with History(Path(self.tmp.name) / 'other.sqlite3') as other:
            other.refresh('claude', src)
            other.snapshot(snap)
        with History(self.db) as h:
            self.assertEqual(h.revision, 0)
            self.assertEqual(h.import_snapshot(snap, 'laptop')['new'], 1)
            first = h.revision
            self.assertGreater(first, 0)
            # Documented: a re-import that adds nothing (identical rows, known sources) leaves the revision alone.
            self.assertEqual(h.import_snapshot(snap, 'laptop')['merged'], 1)
            self.assertEqual(h.revision, first)


class ConditionalReport(Base):
    def setUp(self):
        super().setUp()
        self.claude()
        self.assertEqual(self.run_cli('refresh', '--all')[0], 0)
        self.html = Path(self.tmp.name) / 'out/report.html'

    def report(self, *extra):
        code, out, err = self.run_cli('report', '--html', str(self.html), '--private', *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def age(self, seconds):
        t = time.time() - seconds
        os.utime(self.html, (t, t))

    def change(self):
        self.claude('b.jsonl', request='req9')
        self.run_cli('refresh', '--all')

    def stamp(self):
        s = self.html.stat()
        return s.st_mtime_ns, s.st_ino

    def assertSkipped(self, result, reason):
        self.assertEqual(result, {'html': str(self.html.resolve()), 'skipped': True, 'reason': reason})

    def test_state_marker_in_html(self):
        self.report()
        head = self.html.read_text(encoding='utf-8')[:4096]
        self.assertRegex(head, r'<meta name="tokenatlas-state" content="[0-9a-f]{32}">')
        self.assertEqual(report_mod.read_report_state(self.html), re.search(r'tokenatlas-state" content="([0-9a-f]{32})', head).group(1))

    def test_privacy_change_rebuilds(self):
        self.report('--if-changed')
        code, out, err = self.run_cli('report', '--html', str(self.html), '--if-changed')
        self.assertEqual(code, 0, err)
        self.assertNotIn('skipped', json.loads(out))
        self.assertEqual(payload(self.html.read_text(encoding='utf-8'))['privacy'], 'redacted')

    def test_timezone_and_filter_change_rebuild(self):
        self.report('--if-changed')
        for extra in (('--timezone', 'UTC'), ('--harness', 'claude'), ('--granularity', 'hour')):
            with self.subTest(extra=extra):
                self.report('--if-changed')
                result = self.report('--if-changed', *extra)
                self.assertNotIn('skipped', result)

    def test_coverage_change_without_revision_bump_rebuilds(self):
        self.report('--if-changed')
        rev = self.revision()
        (why.CLAUDE_PROJECTS / 'proj/a.jsonl').unlink()
        self.assertEqual(self.revision(), rev)
        self.assertNotIn('skipped', self.report('--if-changed'))

    def test_different_database_rebuilds(self):
        self.report('--if-changed')
        rev = self.revision()
        other = Path(self.tmp.name) / 'state2'
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(other)}):
            self.assertEqual(self.run_cli('refresh', '--all')[0], 0)
            code, out, err = self.run_cli('report', '--html', str(self.html), '--private', '--if-changed')
        self.assertEqual(code, 0, err)
        self.assertNotIn('skipped', json.loads(out))

    def test_skip_path_never_reads_records(self):
        self.report('--if-changed')
        with patch('tokenatlas.history.History.records', side_effect=AssertionError('records called')):
            self.assertSkipped(self.report('--if-changed'), 'unchanged')

    @unittest.skipUnless(hasattr(os, 'link'), 'needs hard links')
    def test_hard_link_to_database_is_rejected(self):
        link = Path(self.tmp.name) / 'link.html'
        os.link(self.db, link)
        before = self.db.read_bytes()
        self.assertEqual(self.run_cli('report', '--html', str(link))[0], 2)
        with patch.object(cli, '_open_in_browser') as opener:
            self.assertEqual(self.run_cli('open', '--html', str(link), '--no-refresh')[0], 2)
        self.assertEqual(self.db.read_bytes(), before)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs mkfifo')
    def test_fifo_report_path_does_not_block(self):
        fifo = Path(self.tmp.name) / 'fifo.html'
        os.mkfifo(fifo)
        self.assertIsNone(report_mod.read_report_state(fifo))
        self.assertIsNone(report_mod.read_report_state(Path(self.tmp.name)))

    def test_future_mtime_counts_as_stale(self):
        self.report()
        self.change()
        t = time.time() + 10 * 86400
        os.utime(self.html, (t, t))
        self.assertNotIn('skipped', self.report('--if-changed', '--max-age', '1h'))

    def test_no_report_builds_even_with_flags(self):
        result = self.report('--if-changed', '--max-age', '1h')
        self.assertNotIn('skipped', result)
        self.assertTrue(self.html.is_file())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.html.stat().st_mode), 0o600)

    def test_unchanged_skips_and_leaves_file_untouched(self):
        self.report()
        self.age(86400)
        before = self.stamp()
        self.assertSkipped(self.report('--if-changed'), 'unchanged')
        self.assertSkipped(self.report('--if-changed', '--max-age', '1h'), 'unchanged')
        self.assertEqual(self.stamp(), before)

    def test_changed_but_young_skips(self):
        self.report()
        self.change()
        before = self.stamp()
        self.assertSkipped(self.report('--if-changed', '--max-age', '1h'), 'too recent')
        self.assertSkipped(self.report('--max-age', '1h'), 'too recent')
        self.assertEqual(self.stamp(), before)

    def test_changed_and_old_rebuilds(self):
        self.report()
        self.change()
        self.age(7200)
        old = self.stamp()
        result = self.report('--if-changed', '--max-age', '1h')
        self.assertNotIn('skipped', result)
        self.assertNotEqual(self.stamp(), old)
        self.assertIn('tokenatlas-state', self.html.read_text(encoding='utf-8')[:4096])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.html.stat().st_mode), 0o600)

    def test_max_age_alone_rebuilds_when_old_and_if_changed_alone_when_changed(self):
        self.report()
        self.age(7200)
        self.assertNotIn('skipped', self.report('--max-age', '1h'))
        self.change()
        self.assertNotIn('skipped', self.report('--if-changed'))

    def test_bad_duration_and_missing_html(self):
        for bad in ('1w', '5', 'h', '-1h', '1.5h', ''):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli('report', '--html', str(self.html), '--max-age', bad)[0], 2)
        for flag in (('--if-changed',), ('--max-age', '1h')):
            self.assertEqual(self.run_cli('report', *flag)[0], 2)
        self.assertFalse(self.html.exists())

    def test_duration_units(self):
        for text, seconds in (('90s', 90), ('30m', 1800), ('1h', 3600), ('2d', 172800)):
            self.assertEqual(cli.parse_duration(text), seconds)


class OpenCommand(Base):
    def setUp(self):
        super().setUp()
        write_claude(why.CLAUDE_PROJECTS / 'proj/a.jsonl')
        p = patch.object(cli, '_open_in_browser')
        self.opener = p.start()
        self.addCleanup(p.stop)
        self.default = self.state / 'tokenatlas/report.html'

    def test_open_refreshes_private_default_path_and_opens_once(self):
        code, out, err = self.run_cli('open')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['html'], str(self.default.resolve()))
        self.opener.assert_called_once()
        self.assertEqual(Path(self.opener.call_args[0][0]).resolve(), self.default.resolve())
        self.assertIn('claude', err)
        self.assertGreater(self.revision(), 0)
        data = payload(self.default.read_text(encoding='utf-8'))
        self.assertEqual(data['privacy'], 'local')
        self.assertEqual(data['columns']['dict']['project_label'], ['app'])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.default.stat().st_mode), 0o600)

    def test_shared_redacts_and_custom_path(self):
        target = Path(self.tmp.name) / 'x/r.html'
        code, out, _ = self.run_cli('open', '--shared', '--html', str(target))
        self.assertEqual(code, 0)
        data = payload(target.read_text(encoding='utf-8'))
        self.assertEqual(data['privacy'], 'redacted')
        self.assertEqual(data['columns']['dict']['project_label'], ['Projekt 001'])
        self.assertFalse(self.default.exists())
        self.opener.assert_called_once()

    def test_no_refresh_does_not_refresh(self):
        self.assertEqual(self.run_cli('refresh', '--all')[0], 0)
        rev = self.revision()
        write_claude(why.CLAUDE_PROJECTS / 'proj/new.jsonl', request='zzz')
        self.assertEqual(self.run_cli('open', '--no-refresh')[0], 0)
        self.assertEqual(self.revision(), rev)
        self.opener.assert_called_once()

    def test_open_reuses_unchanged_report(self):
        self.assertEqual(self.run_cli('open')[0], 0)
        s = self.default.stat()
        before = (s.st_ino, s.st_mtime_ns)
        with patch('tokenatlas.history.History.records', side_effect=AssertionError('records called')):
            code, out, err = self.run_cli('open', '--no-refresh')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result['skipped'], result['reason']), (True, 'unchanged'))
        s = self.default.stat()
        self.assertEqual((s.st_ino, s.st_mtime_ns), before)
        self.assertEqual(self.opener.call_count, 2)

    def test_open_matches_private_report_state(self):
        self.assertEqual(self.run_cli('refresh', '--all')[0], 0)
        self.assertEqual(self.run_cli('report', '--html', str(self.default), '--private')[0], 0)
        code, out, _ = self.run_cli('open', '--no-refresh')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['reason'], 'unchanged')

    def test_open_continues_when_partial(self):
        with (why.CLAUDE_PROJECTS / 'proj/a.jsonl').open('a') as f:
            f.write('{bad\n')
        code, _, err = self.run_cli('open')
        self.assertEqual(code, 0)
        self.assertIn('partial', err)
        self.opener.assert_called_once()


class BrowserFailure(Base):
    def setUp(self):
        super().setUp()
        write_claude(why.CLAUDE_PROJECTS / 'proj/a.jsonl')

    def test_false_return_and_error_exit_2(self):
        target = Path(self.tmp.name) / 'r.html'
        for kw in ({'return_value': False}, {'side_effect': __import__('webbrowser').Error('no runner')}):
            with self.subTest(kw=kw), patch('webbrowser.open', **kw):
                code, _, err = self.run_cli('open', '--html', str(target))
                self.assertEqual(code, 2)
                self.assertIn(str(target), err)


if __name__ == '__main__':
    unittest.main()
