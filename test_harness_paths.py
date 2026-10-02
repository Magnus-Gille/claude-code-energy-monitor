"""Harness-moved log roots (#52): CLAUDE_CONFIG_DIR, CODEX_HOME, PI_CODING_AGENT_DIR and XDG_DATA_HOME."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenatlas import why
from tokenatlas.turn_context import turn_context

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('demo_script', ROOT / 'scripts/demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

VARS = ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'PI_CODING_AGENT_DIR', 'XDG_DATA_HOME')
# harness -> (variable, location under the demo home, location under the variable's directory)
LAYOUT = {'claude': ('CLAUDE_CONFIG_DIR', '.claude', ''), 'codex': ('CODEX_HOME', '.codex', ''),
          'pi': ('PI_CODING_AGENT_DIR', '.pi/agent', ''), 'opencode': ('XDG_DATA_HOME', '.local/share/opencode', 'opencode')}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.state = self.base / 'state'
        demo_home = self.base / 'demo'
        demo.build_home(demo_home, 1)
        self.demo = demo_home

    def moved(self, harness):
        """Copy one harness's synthetic logs to where its variable points; returns the variable's value."""
        var, src, tail = LAYOUT[harness]
        target = self.base / f'moved-{harness}'
        shutil.copytree(self.demo / src, target / tail)
        return var, str(target)

    def cli(self, *args, env=None):
        full = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                'XDG_STATE_HOME': str(self.state), **(env or {})}
        run = subprocess.run([sys.executable, '-m', 'tokenatlas', *args], cwd=ROOT, env=full, capture_output=True, text=True)
        return run.returncode, run.stdout

    def refresh_all(self, env):
        code, out = self.cli('refresh', '--all', env=env)
        return code, {x['harness']: x for x in json.loads(out)['harnesses']}


class RefreshThroughVariable(Base):
    def test_each_harness_found_through_its_variable(self):
        for harness in LAYOUT:
            with self.subTest(harness=harness):
                var, value = self.moved(harness)
                code, by = self.refresh_all({var: value})
                self.assertEqual(code, 0, by)
                self.assertEqual(by[harness]['status'], 'ok')
                self.assertGreater(by[harness]['observations_seen'], 0)
                for other in set(LAYOUT) - {harness}:
                    self.assertEqual(by[other]['status'], 'absent')

    def test_empty_or_relative_variable_means_default(self):
        for src in ('.claude', '.codex', '.pi', '.local'):
            shutil.copytree(self.demo / src, self.home / src)
        for value in ('', 'rel'):
            with self.subTest(value=value):
                code, by = self.refresh_all({var: value for var in VARS})
                self.assertEqual(code, 0, by)
                self.assertEqual({h: x['status'] for h, x in by.items()}, {h: 'ok' for h in LAYOUT})

    def test_explicit_root_beats_variable(self):
        var, value = self.moved('claude')
        explicit = self.base / 'explicit'
        shutil.copytree(self.demo / '.claude/projects', explicit)
        for p in list((Path(value) / 'projects').glob('*')):
            shutil.rmtree(p)  # the variable's root is now empty: only the explicit root can yield data
        code, out = self.cli('refresh', '--harness', 'claude', '--root', str(explicit), env={var: value})
        self.assertEqual(code, 0, out)
        self.assertGreater(json.loads(out)['observations_seen'], 0)


class Resolver(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.x = Path(tmp.name).resolve() / 'x'  # platform-native absolute path

    def resolve(self, name, **env):
        clean = {k: v for k, v in os.environ.items() if k not in VARS}
        with patch.dict(os.environ, {**clean, **env}, clear=True):
            return why.harness_root(name)

    def test_variable_and_default(self):
        x = self.x
        self.assertEqual(self.resolve('claude', CLAUDE_CONFIG_DIR=str(x)), (x / 'projects', 'CLAUDE_CONFIG_DIR'))
        self.assertEqual(self.resolve('codex', CODEX_HOME=str(x)), (x / 'sessions', 'CODEX_HOME'))
        self.assertEqual(self.resolve('pi', PI_CODING_AGENT_DIR=str(x)), (x / 'sessions', 'PI_CODING_AGENT_DIR'))
        self.assertEqual(self.resolve('opencode', XDG_DATA_HOME=str(x)), (x / 'opencode' / 'opencode.db', 'XDG_DATA_HOME'))
        self.assertEqual(self.resolve('pi'), (why.PI_SESSIONS, 'default'))

    def test_empty_and_relative_are_default(self):
        defaults = {'claude': why.CLAUDE_PROJECTS, 'codex': why.CODEX_SESSIONS, 'pi': why.PI_SESSIONS, 'opencode': why.OPENCODE_DB}
        for name, (var, _, _) in LAYOUT.items():
            for value in ('', 'rel/dir'):
                with self.subTest(harness=name, value=value):
                    self.assertEqual(self.resolve(name, **{var: value}), (defaults[name], 'default'))

    def test_pi_expands_leading_tilde(self):
        with patch.dict(os.environ, {'HOME': str(self.x), 'USERPROFILE': str(self.x), 'PI_CODING_AGENT_DIR': '~/agent'}):
            expected = Path('~/agent').expanduser() / 'sessions'
            self.assertEqual(why.harness_root('pi'), (expected, 'PI_CODING_AGENT_DIR'))
            self.assertEqual(expected, self.x / 'agent' / 'sessions')

    def test_pi_unknown_user_tilde_falls_back_to_default(self):
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': '~nonexistent-user-xyz/agent'}):
            self.assertEqual(why.harness_root('pi'), (why.PI_SESSIONS, 'default'))


class CodexIndex(unittest.TestCase):
    def test_session_index_follows_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / 'codex'
            with patch.dict(os.environ, {'CODEX_HOME': str(home)}):
                self.assertEqual(why.codex_session_index(), home / 'session_index.jsonl')
        with patch.dict(os.environ, {'CODEX_HOME': ''}):
            self.assertEqual(why.codex_session_index(), why.CODEX_SESSIONS.parent / 'session_index.jsonl')

    def test_turn_context_reads_index_from_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CODEX_HOME': tmp}), \
                patch('tokenatlas.turn_context._codex', return_value={}) as codex:
            turn_context('codex', ['s'], 'cs1', 'T1')
        self.assertEqual(codex.call_args[0][3], Path(tmp) / 'session_index.jsonl')


class Doctor(Base):
    def test_doctor_reports_path_and_source(self):
        self.cli('refresh', '--all')  # creates the (empty) history; doctor needs one
        codex = self.base / 'codex'
        code, out = self.cli('doctor', env={'CODEX_HOME': str(codex), 'PI_CODING_AGENT_DIR': ''})
        self.assertEqual(code, 0, out)
        roots = json.loads(out)['roots']
        self.assertEqual(roots['codex'], {'path': str(codex / 'sessions'), 'source': 'CODEX_HOME'})
        self.assertEqual(roots['pi'], {'path': str(self.home / '.pi/agent/sessions'), 'source': 'default'})
        self.assertEqual(set(roots), set(LAYOUT))


if __name__ == '__main__':
    unittest.main()
