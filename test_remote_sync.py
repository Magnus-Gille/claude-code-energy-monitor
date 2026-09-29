"""remote_sync.sh: entry validation and per-host failure isolation, with stubbed commands on a temp PATH."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('remote_sync.sh')
STUB = '#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n'


@unittest.skipIf(os.name == 'nt', 'remote_sync.sh targets macOS/Linux hosts')
class RemoteSyncTest(unittest.TestCase):
    def run_script(self, hosts, mv_fails_first=False, rm_fails=False, extra_env=None, em_in_home_bin=False, remote_only=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'bin').mkdir()
        (root / 'home').mkdir()
        log = root / 'log'
        counter = root / 'mv_count'
        bodies = {'ssh': 'exit 0', 'rsync': 'exit 0', 'tokenatlas': 'exit 0', 'scp': 'touch "${@: -1}"',
                  'rm': 'exit 1' if rm_fails else 'exit 0',
                  'mv': (f'if [ ! -e "{counter}" ]; then touch "{counter}"; exit 1; fi; exit 0'
                         if mv_fails_first else 'exit 0')}
        if remote_only:
            # ssh really runs the remote command line in a fake remote home holding only `remote_only`.
            remote_bin = root / 'remote_home' / '.local' / 'bin'
            remote_bin.mkdir(parents=True)
            (remote_bin / remote_only).write_text(f'#!/bin/bash\necho "remote-{remote_only} $*" >> "$STUB_LOG"\nexit 0\n')
            (remote_bin / remote_only).chmod(0o755)
            bodies['ssh'] = ('HOME="' + str(root / 'remote_home') + '" PATH=/usr/bin:/bin bash -c "$3"')
        for name, body in bodies.items():
            if name == 'tokenatlas' and em_in_home_bin:
                (root / 'home' / '.local' / 'bin').mkdir(parents=True)
                stub = root / 'home' / '.local' / 'bin' / name
            else:
                stub = root / 'bin' / name
            stub.write_text(STUB.format(name=name, body=body))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {'PATH': f'{root / "bin"}:/usr/bin:/bin', 'HOME': str(root / 'home'), 'STUB_LOG': str(log),
               'REMOTE_HOSTS_OVERRIDE': hosts, **(extra_env or {})}
        proc = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        return proc, log.read_text() if log.exists() else ''

    def test_invalid_pairs_rejected(self):
        proc, log = self.run_script('../../x:host pi:-oProxyCommand=x ok:good.host')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("invalid tag:host entry '../../x:host'", proc.stderr)
        self.assertIn("invalid tag:host entry 'pi:-oProxyCommand=x'", proc.stderr)
        self.assertNotIn('../../x', log)
        self.assertNotIn('ProxyCommand', log)
        self.assertIn('rsync -az -- good.host:~/.claude/pi_journal.jsonl', log)
        self.assertIn('ssh -- good.host', log)

    def test_failing_mv_does_not_stop_next_host(self):
        proc, log = self.run_script('a:h1 b:h2', mv_fails_first=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('cannot store snapshot for a', proc.stderr)
        self.assertIn('Syncing energy data from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)
        self.assertIn('tokenatlas import', log)

    def test_failing_rm_cleanup_does_not_stop_next_host(self):
        proc, log = self.run_script('a:h1 b:h2', mv_fails_first=True, rm_fails=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('cannot store snapshot for a', proc.stderr)
        self.assertIn('Syncing energy data from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)

    def test_remote_commands_prepend_user_bin_to_path(self):
        proc, log = self.run_script('a:h1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ssh_lines = [line for line in log.splitlines() if line.startswith('ssh -- h1')]
        self.assertEqual(len(ssh_lines), 2, ssh_lines)
        check, snap = ssh_lines
        self.assertIn('PATH="$HOME/.local/bin:$PATH"', check)
        self.assertIn('command -v tokenatlas || command -v energy-monitor', check)
        self.assertIn('PATH="$HOME/.local/bin:$PATH"', snap)
        self.assertIn('snapshot ~/.local/state/tokenatlas/snapshot.sqlite3', snap)

    def test_local_db_option_before_import(self):
        proc, log = self.run_script('a:h1', extra_env={'TOKENATLAS_DB': '/tmp/x.sqlite3'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('tokenatlas --db /tmp/x.sqlite3 import ', log)

    def test_legacy_env_db_still_honoured(self):
        proc, log = self.run_script('a:h1', extra_env={'ENERGY_MONITOR_DB': '/tmp/y.sqlite3'})
        self.assertIn('tokenatlas --db /tmp/y.sqlite3 import ', log)

    def test_remote_with_only_energy_monitor_falls_back(self):
        proc, log = self.run_script('a:h1', remote_only='energy-monitor')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('remote-energy-monitor snapshot', log)
        self.assertIn('history: OK', proc.stdout)

    def test_remote_with_tokenatlas_uses_it(self):
        proc, log = self.run_script('a:h1', remote_only='tokenatlas')
        self.assertIn('remote-tokenatlas snapshot', log)
        self.assertIn('history: OK', proc.stdout)

    def test_no_db_option_by_default(self):
        proc, log = self.run_script('a:h1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('tokenatlas import ', log)
        self.assertNotIn('--db', log)

    def test_empty_db_env_means_default(self):
        proc, log = self.run_script('a:h1', extra_env={'ENERGY_MONITOR_DB': ''})
        self.assertNotIn('--db', log)

    def test_local_user_bin_install_found_without_path(self):
        proc, log = self.run_script('a:h1', em_in_home_bin=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('not installed locally', proc.stderr)
        self.assertIn('tokenatlas import ', log)
        self.assertIn('history: OK', proc.stdout)


if __name__ == '__main__':
    unittest.main()
