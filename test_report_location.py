"""Where the report is saved is visible (#64): a stderr line from report/open, the private report's footer, and --help; never in a shared report."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_fresh_report import payload

ROOT = Path(__file__).parent
JOIN = shlex.join if os.name != 'nt' else subprocess.list2cmdline  # as the CLI quotes the reopen command
ROW = {'type': 'assistant', 'uuid': 'r1', 'requestId': 'q1', 'sessionId': 's1', 'cwd': '/w/app', 'version': 'test', 'timestamp': '2026-09-03T10:00:00Z',
       'message': {'id': 'm1', 'model': 'claude-sonnet-4-5', 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 0, 'output_tokens': 5}}}


class ReportLocation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / 'home').mkdir()
        logs = self.tmp / 'logs'
        logs.mkdir()
        (logs / 's1.jsonl').write_text(json.dumps(ROW) + '\n', encoding='utf-8')
        self.db = self.tmp / 'state' / 'history.sqlite3'
        self.env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'PI_CODING_AGENT_DIR', 'XDG_DATA_HOME')}
        self.env.update(HOME=str(self.tmp / 'home'), USERPROFILE=str(self.tmp / 'home'), XDG_STATE_HOME=str(self.tmp / 'state-home'),
                        BROWSER='true', PYTHONPATH=str(ROOT))
        self.run_cli('refresh', '--harness', 'claude', '--root', str(logs))

    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(self.db), *args], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_private_report_names_its_file_and_the_shared_one_never_does(self):
        private, shared = self.tmp / 'private.html', self.tmp / 'shared.html'
        proc = self.run_cli('report', '--html', str(private), '--private')
        self.assertIn(f'Report: {private}', proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['html'], str(private))  # stdout stays the JSON receipt
        self.assertEqual(payload(private.read_text(encoding='utf-8'))['saved_at'], str(private))
        self.run_cli('report', '--html', str(shared))
        html = shared.read_text(encoding='utf-8')
        self.assertNotIn('saved_at', payload(html))
        self.assertNotIn(str(self.tmp), html)

    def test_report_names_a_reopen_command_only_for_opens_own_file(self):
        custom = self.tmp / 'custom.html'
        self.run_cli('report', '--html', str(custom), '--private')
        self.assertIsNone(payload(custom.read_text(encoding='utf-8'))['reopen'])  # open would rebuild it with other options
        default = self.db.parent / 'report.html'
        self.run_cli('report', '--html', str(default), '--private')
        self.assertEqual(payload(default.read_text(encoding='utf-8'))['reopen'], JOIN(['tokenatlas', '--db', str(self.db), 'open']))

    def test_a_moved_copy_is_rebuilt_with_its_own_path(self):
        first, moved = self.tmp / 'first.html', self.tmp / 'moved.html'
        self.run_cli('open', '--html', str(first), '--no-refresh')
        shutil.copy2(first, moved)
        self.run_cli('open', '--html', str(moved), '--no-refresh')
        self.assertEqual(payload(moved.read_text(encoding='utf-8'))['saved_at'], str(moved))

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_a_symlinked_destination_names_the_path_that_is_written(self):
        target, link = self.tmp / 'target.html', self.tmp / 'link.html'
        target.write_text('old', encoding='utf-8')
        link.symlink_to(target)
        self.run_cli('report', '--html', str(link), '--private')
        self.assertEqual(payload(link.read_text(encoding='utf-8'))['saved_at'], str(link))

    def test_windows_shows_a_reopen_command_only_for_plain_paths(self):
        from tokenatlas import __main__ as cli
        self.assertIsNone(cli._shell_command(['tokenatlas', 'open', '--html', r'C:\a&b\r.html'], windows=True))  # & would split it in cmd.exe
        self.assertIsNone(cli._shell_command(['tokenatlas', 'open', '--html', r'C:\100%\r.html'], windows=True))
        self.assertEqual(cli._shell_command(['tokenatlas', 'open', '--html', r'C:\my reports\r.html'], windows=True),
                         r'tokenatlas open --html "C:\my reports\r.html"')
        self.assertEqual(cli._shell_command(['tokenatlas', 'open', '--html', '/a&b/r.html'], windows=False), "tokenatlas open --html '/a&b/r.html'")

    def test_skipped_report_still_says_where_it_is(self):
        out = self.tmp / 'r.html'
        self.run_cli('report', '--html', str(out), '--private', '--if-changed')
        proc = self.run_cli('report', '--html', str(out), '--private', '--if-changed')
        self.assertIn(f'Report: {out} (unchanged)', proc.stderr)

    def test_open_prints_the_path_and_the_exact_reopen_command(self):
        out = self.tmp / 'open.html'
        proc = self.run_cli('open', '--html', str(out), '--no-refresh')
        cmd = JOIN(['tokenatlas', '--db', str(self.db), 'open', '--html', str(out)])
        self.assertIn(f'Report: {out} (reopen any time with: {cmd})', proc.stderr)
        data = payload(out.read_text(encoding='utf-8'))
        self.assertEqual((data['saved_at'], data['reopen']), (str(out), cmd))
        default = self.db.parent / 'report.html'
        proc = self.run_cli('open', '--no-refresh')
        self.assertIn(f"reopen any time with: {JOIN(['tokenatlas', '--db', str(self.db), 'open'])})", proc.stderr)
        self.assertEqual(payload(default.read_text(encoding='utf-8'))['saved_at'], str(default))
        shared = self.tmp / 'open-shared.html'
        self.run_cli('open', '--html', str(shared), '--no-refresh', '--shared')
        self.assertNotIn(str(self.tmp), shared.read_text(encoding='utf-8'))

    def test_help_shows_the_resolved_default(self):
        proc = subprocess.run([sys.executable, '-m', 'tokenatlas', 'open', '--help'], cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertNotIn('$XDG_STATE_HOME', proc.stdout)
        self.assertIn('report.html', proc.stdout)
        self.assertIn('state-home', proc.stdout.replace('\n', '').replace(' ', ''))

    def test_template_and_texts(self):
        template = (ROOT / 'tokenatlas' / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn('<span id="saved-at"></span>', template)
        self.assertIn("t('saved_at_reopen',{path:DATA.saved_at,cmd:DATA.reopen})", template)
        texts = json.loads((ROOT / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        for lang in ('sv', 'en'):
            self.assertIn('{path}', texts[lang]['saved_at'])
            self.assertEqual({'{path}', '{cmd}'} <= set(__import__('re').findall(r'\{\w+\}', texts[lang]['saved_at_reopen'])), True)


if __name__ == '__main__':
    unittest.main()
