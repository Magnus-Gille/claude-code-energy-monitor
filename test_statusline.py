import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tokenatlas import statusline
from tokenatlas.__main__ import main, refresh_all
from tokenatlas.history import History

ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)  # 12:00 in Europe/Stockholm
USAGE = {'input_tokens': 1000, 'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 100, 'output_tokens': 500}
OPUS_MWH = 1000 / 1000 * 390 + 2000 / 1000 * 15 + 100 / 1000 * 490 + 500 / 1000 * 1400  # 1169 mWh at multiplier 1


def assistant(request, stamp, model, usage=USAGE, ambiguous=False):
    row = {'type': 'assistant', 'uuid': 'row-' + request, 'requestId': request, 'sessionId': 'session', 'cwd': '/work/app',
           'version': 'test-version', 'timestamp': stamp,
           'message': {'id': 'msg-' + request, 'model': model, 'stop_reason': 'end_turn', 'usage': usage}}
    if ambiguous:
        row.pop('requestId')
        row['message'].pop('id')
    return row


def write_claude(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


ROWS = (assistant('a1', '2026-09-30T10:00:00Z', 'claude-opus-4-8'),  # Sep 30 local
        assistant('a2', '2026-09-30T23:30:00Z', 'claude-sonnet-5'),  # Oct 1 01:30 local: the next local day
        assistant('a3', '2026-10-01T08:00:00Z', 'mystery-model'),  # unweighted
        assistant('a4', '2026-10-01T08:30:00Z', 'claude-opus-4-8', {}),  # no counters: incomplete
        assistant('a5', '2026-10-01T09:00:00Z', 'claude-opus-4-8', ambiguous=True),  # ambiguous identity: excluded
        assistant('a6', '2026-08-01T10:00:00Z', 'claude-opus-4-8'))  # outside the 31 days


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'state' / 'history.sqlite3'
        self.logs = self.root / 'logs'
        write_claude(self.logs / 'a.jsonl', ROWS)
        if hasattr(time, 'tzset'):  # bucketing uses the machine's local zone: pin it
            previous = os.environ.get('TZ')
            os.environ['TZ'] = 'Europe/Stockholm'
            time.tzset()
            self.addCleanup(self.restore_zone, previous)

    @staticmethod
    def restore_zone(previous):
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--db', str(self.db), *args])
        return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class CacheTests(Base):
    def build(self):
        with History(self.db) as h:
            h.refresh('claude', self.logs)
            return statusline.build_cache(h, NOW), h.revision

    def test_buckets_by_local_day_and_counts_each_class(self):
        cache, revision = self.build()
        self.assertEqual(sorted(cache['days']), ['2026-09-30', '2026-10-01'])  # the August row is outside 31 days
        self.assertEqual(cache['revision'], revision)
        self.assertEqual(cache['written_at'], '2026-10-01T10:00:00+00:00')
        sep30, oct1 = cache['days']['2026-09-30'], cache['days']['2026-10-01']
        self.assertEqual({k: sep30[k] for k in statusline.CLASSES}, dict(fresh_input=1000, cache_read=2000, cache_write=100, output=500))
        self.assertEqual((sep30['requests'], sep30['unweighted'], sep30['incomplete']), (1, 0, 0))
        # a2 (sonnet, 23:30Z = 01:30 local), a3 (unknown model), a4 (no counters): ambiguous a5 is not here
        self.assertEqual({k: oct1[k] for k in statusline.CLASSES}, dict(fresh_input=2000, cache_read=4000, cache_write=200, output=1000))
        self.assertEqual((oct1['requests'], oct1['unweighted'], oct1['incomplete']), (3, 1, 1))

    def test_energy_uses_the_model_multiplier(self):
        cache, _ = self.build()
        self.assertAlmostEqual(cache['days']['2026-09-30']['mwh'], OPUS_MWH)
        self.assertAlmostEqual(cache['days']['2026-10-01']['mwh'], OPUS_MWH * 0.6 + OPUS_MWH * 1.0)

    def test_the_fixture_has_an_ambiguous_row(self):
        with History(self.db) as h:
            h.refresh('claude', self.logs)
            self.assertEqual(sum(r['id_synthetic'] for r in h.records()), 1)

    def test_write_is_private_atomic_and_leaves_no_temp_file(self):
        path = self.root / 'out' / statusline.CACHE_NAME
        path.parent.mkdir()
        statusline.write_atomic(path, {'v': 1})
        self.assertEqual(json.loads(path.read_text()), {'v': 1})
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with mock.patch('os.replace', side_effect=OSError('boom')), self.assertRaises(OSError):
            statusline.write_atomic(path, {'v': 2})
        self.assertEqual(json.loads(path.read_text()), {'v': 1})  # the old file survives a failed write
        self.assertEqual([p.name for p in path.parent.iterdir()], [statusline.CACHE_NAME])


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class RefreshTests(Base):
    def test_refresh_writes_the_cache_next_to_the_database(self):
        code, _, _ = self.cli('refresh', '--harness', 'claude', '--root', str(self.logs))
        self.assertEqual(code, 0)
        cache = json.loads((self.db.parent / statusline.CACHE_NAME).read_text())
        self.assertGreaterEqual(cache['revision'], 1)
        self.assertIn('days', cache)

    def test_refresh_all_writes_the_cache(self):
        with mock.patch('tokenatlas.why.harness_root', return_value=(self.root / 'absent', 'test')), \
                mock.patch('tokenatlas.why.cowork_scan', return_value=([], [])), History(self.db) as h:
            refresh_all(h)
        self.assertTrue((self.db.parent / statusline.CACHE_NAME).is_file())

    def test_a_cache_failure_warns_and_does_not_fail_refresh(self):
        with mock.patch.object(statusline, 'build_cache', side_effect=RuntimeError('boom')):
            code, out, err = self.cli('refresh', '--harness', 'claude', '--root', str(self.logs))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'ok')
        self.assertIn('warning: could not write statusline.json: RuntimeError: boom', err)
        self.assertFalse((self.db.parent / statusline.CACHE_NAME).exists())


CACHE = {'v': 1, 'written_at': '2026-10-01T09:50:00+00:00', 'revision': 3, 'days': {
    '2026-10-01': dict(fresh_input=1000, cache_read=1_000_000, cache_write=0, output=1000, mwh=2000.0, requests=2, unweighted=0, incomplete=0),
    '2026-09-28': dict(fresh_input=0, cache_read=0, cache_write=0, output=5_000_000, mwh=7_000_000.0, requests=9, unweighted=0, incomplete=0),
    '2026-09-10': dict(fresh_input=0, cache_read=0, cache_write=0, output=2_000_000_000, mwh=3_000_000_000.0, requests=9, unweighted=0, incomplete=0)}}
PAYLOAD = {'model': {'display_name': 'Opus 4.8'}, 'context_window': {'used_percentage': 42.4},
           'rate_limits': {'five_hour': {'used_percentage': 29.2}, 'seven_day': {'used_percentage': 51.6}}}


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class StatuslineTests(Base):
    def line(self, payload=PAYLOAD, cache=CACHE, now=NOW, raw=None):
        db = self.db
        if cache is not None:
            db.parent.mkdir(parents=True, exist_ok=True)
            (db.parent / statusline.CACHE_NAME).write_text(json.dumps(cache))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = statusline.run([], db, io.StringIO(json.dumps(payload) if raw is None else raw), now)
        self.assertEqual(code, 0)
        return out.getvalue().rstrip('\n')

    def test_full_payload(self):
        self.assertEqual(self.line(), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52% | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_month_window_is_thirty_days_and_week_is_seven(self):
        # 2026-09-10 is 21 days back (in the month, not the week); 2026-08-31 is 31 days back (in neither)
        old = dict(fresh_input=0, cache_read=0, cache_write=0, output=1_000_000_000, mwh=1e9, requests=1, unweighted=0, incomplete=0)
        cache = dict(CACHE, days=dict(CACHE['days'], **{'2026-08-31': old}))
        self.assertEqual(self.line(cache=cache), self.line())
        self.assertIn('| W:6.0M ~5 kWh | M:2.0B ~2 MWh', self.line())

    def test_without_rate_limits(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != 'rate_limits'}
        self.assertEqual(self.line(payload), 'Opus 4.8 | Ctx:42% | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_unknown_model_and_minimal_payload(self):
        self.assertEqual(self.line({}), '? | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_missing_cache_omits_the_totals(self):
        self.assertEqual(self.line(cache=None), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52%')

    def test_unreadable_cache_omits_the_totals(self):
        self.db.parent.mkdir(parents=True)
        (self.db.parent / statusline.CACHE_NAME).write_text('{not json')
        self.assertEqual(self.line(cache=None), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52%')

    def test_stale_cache_appends_its_local_time(self):
        stale = dict(CACHE, written_at='2026-10-01T08:40:00+00:00')  # 80 minutes before NOW; 10:40 in Stockholm
        self.assertTrue(self.line(cache=stale).endswith('M:2.0B ~2 MWh (10:40)'))
        fresh = dict(CACHE, written_at='2026-10-01T09:20:00+00:00')  # 40 minutes: still fresh
        self.assertNotIn('(', self.line(cache=fresh))

    def test_midnight_rolls_the_day_total_without_a_rewrite(self):
        line = self.line(now=datetime(2026, 10, 1, 22, 30, tzinfo=timezone.utc))  # 00:30 on Oct 2 locally
        self.assertIn('| D:0 |', line)
        self.assertIn('W:6.0M ~5 kWh', line)

    def test_garbage_stdin_prints_a_fallback_and_exits_zero(self):
        self.assertEqual(self.line(raw='not json'), 'TokenAtlas')
        self.assertEqual(self.line(raw=''), 'TokenAtlas')

    def test_a_bad_value_falls_back_to_the_model_name(self):
        self.assertEqual(self.line({'model': {'display_name': 'Opus 4.8'}, 'context_window': {'used_percentage': 'x'}}), 'Opus 4.8')

    def test_it_writes_nothing(self):
        self.line()
        before = sorted(p.name for p in self.db.parent.iterdir())
        mtime = (self.db.parent / statusline.CACHE_NAME).stat().st_mtime_ns
        self.line(cache=None)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before)
        self.assertEqual((self.db.parent / statusline.CACHE_NAME).stat().st_mtime_ns, mtime)

    def test_subprocess_prints_garbage_fallback_and_exits_zero(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(self.db), 'statusline'], input='\x00garbage', text=True,
                              capture_output=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (0, 'TokenAtlas', ''))


class LightImportTests(unittest.TestCase):
    def test_statusline_does_not_import_the_heavy_modules(self):
        code = ('import sys, io\nfrom tokenatlas.__main__ import main\nsys.stdin = io.StringIO("{}")\nmain(["statusline"])\n'
                'heavy = [m for m in ("history", "report", "insights", "pricing", "why", "sessions", "prompt_store") if "tokenatlas." + m in sys.modules]\n'
                'print("HEAVY", heavy)\n')
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT,
                                  env=dict(os.environ, PYTHONPATH=str(ROOT), XDG_STATE_HOME=tmp))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.rstrip().endswith('HEAVY []'), done.stdout)


class SetupTests(unittest.TestCase):
    def test_setup_prints_the_snippet_and_never_edits_the_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tmp}), mock.patch.object(statusline, 'executable', return_value='/opt/bin/tokenatlas'), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(statusline.run(['--setup']), 0)
            self.assertEqual(os.listdir(tmp), [])
        text = out.getvalue()
        self.assertIn(str(Path(tmp) / 'settings.json'), text)
        snippet = json.loads(text[text.index('{'):text.rindex('}') + 1])
        self.assertEqual(snippet, {'statusLine': {'type': 'command', 'command': '/opt/bin/tokenatlas statusline'}})

    def test_setup_default_location_and_custom_database(self):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
        with mock.patch.dict(os.environ, env, clear=True):
            text = statusline.setup_text('/data/h.sqlite3', '/opt/my bin/tokenatlas')
        self.assertIn(str(Path.home() / '.claude' / 'settings.json'), text)
        self.assertIn("'/opt/my bin/tokenatlas' --db /data/h.sqlite3 statusline", text)

    def test_setup_through_the_cli(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', 'statusline', '--setup'], capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT), CLAUDE_CONFIG_DIR='/nonexistent/claude'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('/nonexistent/claude/settings.json', done.stdout)
        self.assertIn('"statusLine"', done.stdout)


if __name__ == '__main__':
    unittest.main()
