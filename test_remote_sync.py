"""remote_sync.sh: entry validation and per-host failure isolation, with stubbed commands on a temp PATH."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('remote_sync.sh')
STUB = '#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n'


class RemoteSyncTest(unittest.TestCase):
    def run_script(self, hosts, mv_fails_first=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'bin').mkdir()
        (root / 'home').mkdir()
        log = root / 'log'
        counter = root / 'mv_count'
        bodies = {'ssh': 'exit 0', 'rsync': 'exit 0', 'energy-monitor': 'exit 0', 'scp': 'touch "${@: -1}"',
                  'mv': (f'if [ ! -e "{counter}" ]; then touch "{counter}"; exit 1; fi; exit 0'
                         if mv_fails_first else 'exit 0')}
        for name, body in bodies.items():
            stub = root / 'bin' / name
            stub.write_text(STUB.format(name=name, body=body))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {'PATH': f'{root / "bin"}:/usr/bin:/bin', 'HOME': str(root / 'home'), 'STUB_LOG': str(log),
               'REMOTE_HOSTS_OVERRIDE': hosts}
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
        self.assertIn('energy-monitor import', log)


if __name__ == '__main__':
    unittest.main()
