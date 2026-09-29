import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tokenatlas.__main__ import main
from tokenatlas.history import History


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class DataDirMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / 'state'
        self.state.mkdir()
        self.old, self.new = self.state / 'agentmon', self.state / 'tokenatlas'
        patcher = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_old(self):
        self.old.mkdir()
        with History(self.old / 'history.sqlite3'):
            pass
        (self.old / 'outcomes.jsonl').write_text('{}\n')
        (self.old / 'remote').mkdir()

    def test_migration_moves_directory_and_db_stays_readable(self):
        self.make_old()
        code, out, err = run('doctor')
        self.assertEqual(code, 0)
        self.assertFalse(self.old.exists())
        self.assertEqual(err.strip(), f'moved history from {self.old} to {self.new}')
        for name in ('history.sqlite3', 'outcomes.jsonl', 'remote'):
            self.assertTrue((self.new / name).exists(), name)
        with closing(sqlite3.connect(self.new / 'history.sqlite3')) as conn:  # `with conn` alone does not close it
            conn.execute('select count(*) from sqlite_master').fetchone()

    def test_existing_new_directory_wins_with_warning(self):
        self.make_old()
        self.new.mkdir()
        with History(self.new / 'history.sqlite3'):
            pass
        code, out, err = run('doctor')
        self.assertEqual(code, 0)
        self.assertTrue(self.old.is_dir())
        self.assertIn('left in place', err)
        self.assertNotIn('moved history', err)

    def test_explicit_db_skips_migration(self):
        self.make_old()
        db = Path(self._tmp.name) / 'explicit.sqlite3'
        with History(db):
            pass
        code, out, err = run('--db', str(db), 'doctor')
        self.assertEqual(code, 0)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(err, '')

    def test_xdg_state_home_is_respected_for_fresh_install(self):
        code, out, err = run('refresh', '--harness', 'claude', '--root', str(self.state / 'none'))
        self.assertTrue((self.new / 'history.sqlite3').is_file())
        self.assertNotIn('moved history', err)

    def test_deprecated_alias_prints_one_line(self):
        with patch('sys.argv', ['/x/bin/energy-monitor']):
            code, out, err = run('--db', str(self.state / 'x.sqlite3'), 'refresh', '--harness', 'claude', '--root', str(self.state / 'none'))
        self.assertEqual(err.count('energy-monitor is deprecated; use tokenatlas'), 1)


if __name__ == '__main__':
    unittest.main()
