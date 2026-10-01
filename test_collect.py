"""scripts/collect.sh: lock, step order and bounded remote sync, with fake tokenatlas and remote_sync.sh."""
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

COLLECT = Path(__file__).with_name('scripts') / 'collect.sh'


def kill_group(proc):
    """Kill the whole process group of a start_new_session child and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def proc_start(pid):
    return subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


@unittest.skipIf(os.name == 'nt', 'collect.sh targets macOS/Linux')
class CollectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.log = self.root / 'calls'
        (self.root / 'bin').mkdir()
        (self.root / 'home').mkdir()
        self.write_exe(self.root / 'bin' / 'tokenatlas',
                       '#!/bin/bash\necho "$(python3 -c \'import time;print(time.time())\') tokenatlas $*" >> "$CALLS"\n'
                       'exit "${FAKE_TA_RC:-0}"\n')
        self.sync = self.root / 'remote_sync.sh'
        self.write_exe(self.sync, '#!/bin/bash\necho "$(python3 -c \'import time;print(time.time())\') remote_sync" >> "$CALLS"\n'
                                  'exit "${FAKE_SYNC_RC:-0}"\n')

    @staticmethod
    def write_exe(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def run_collect(self, **extra):
        env = {'PATH': f'{self.root / "bin"}:/usr/bin:/bin', 'HOME': str(self.root / 'home'),
               'TOKENATLAS_STATE_DIR': str(self.state), 'TOKENATLAS_REMOTE_SYNC': str(self.sync),
               'CALLS': str(self.log), **extra}
        proc = subprocess.Popen(['bash', str(COLLECT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=60)
        finally:
            kill_group(proc)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def spawn_holder(self):
        """A live process in its own session; returns (pid, its start time as ps reports it)."""
        holder = subprocess.Popen(['sleep', '30'], start_new_session=True)
        self.addCleanup(kill_group, holder)
        return holder.pid, proc_start(holder.pid)

    @staticmethod
    def write_owner(lock, pid, start, nonce='n0nce'):
        lock.mkdir(parents=True, exist_ok=True)
        (lock / 'owner').write_text(f'{pid}\n{start}\n{nonce}\n')

    def calls(self):
        return [l.split(' ', 1)[1] for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_syntax(self):
        subprocess.run(['bash', '-n', str(COLLECT)], check=True)

    def test_local_report_built_before_remote_sync(self):
        proc = self.run_collect(REMOTE_HOSTS_OVERRIDE='a:h1')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], 'tokenatlas refresh --all')
        self.assertTrue(calls[1].startswith('tokenatlas report --html '), calls)
        self.assertIn('--private --if-changed --max-age 1h', calls[1])
        self.assertEqual(calls[2], 'remote_sync')
        self.assertTrue(calls[3].startswith('tokenatlas report --html '), calls)
        self.assertEqual(len(calls), 4)
        stamps = [float(l.split(' ', 1)[0]) for l in self.log.read_text().splitlines()]
        self.assertEqual(stamps, sorted(stamps))
        self.assertIn('refresh exit=0', proc.stdout)
        self.assertIn('remote-sync exit=0', proc.stdout)
        self.assertFalse((self.state / 'collect.lock').exists(), 'lock must be released')

    def test_failed_remote_sync_skips_second_report_but_local_ran(self):
        proc = self.run_collect(REMOTE_HOSTS_OVERRIDE='a:h1', FAKE_SYNC_RC='1')
        self.assertEqual(proc.returncode, 1)
        calls = self.calls()
        self.assertEqual([c.split()[1] if c.startswith('tokenatlas') else c for c in calls],
                         ['refresh', 'report', 'remote_sync'])

    def test_no_remote_sync_without_hosts_and_top_only_when_opted_in(self):
        self.run_collect()
        self.assertNotIn('remote_sync', self.calls())
        self.assertFalse(any(' top' in c for c in self.calls()))
        (self.state / 'top-prompts.json').write_text('{}')
        self.log.unlink()
        self.run_collect()
        self.assertEqual([c.split()[1] for c in self.calls()], ['refresh', 'top', 'report'])

    def test_live_verified_owner_blocks_second_run(self):
        lock = self.state / 'collect.lock'
        pid, start = self.spawn_holder()
        self.write_owner(lock, pid, start)
        start_t = time.monotonic()
        proc = self.run_collect(REMOTE_HOSTS_OVERRIDE='a:h1')
        self.assertLess(time.monotonic() - start_t, 5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('already running', proc.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue(lock.exists(), 'a foreign lock must stay')

    def test_stale_lock_with_dead_pid_is_taken_over(self):
        lock = self.state / 'collect.lock'
        dead = subprocess.Popen(['true'])
        dead.wait()
        self.write_owner(lock, dead.pid, 'Thu Jan  1 00:00:00 1970')
        proc = self.run_collect()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('taking over stale lock', proc.stdout)
        self.assertEqual(self.calls()[0], 'tokenatlas refresh --all')
        self.assertFalse(lock.exists())

    def test_pid_reuse_lock_is_taken_over(self):
        lock = self.state / 'collect.lock'
        pid, _ = self.spawn_holder()  # alive, but recorded with a different start time
        self.write_owner(lock, pid, 'Thu Jan  1 00:00:00 1970')
        proc = self.run_collect()
        self.assertIn('taking over stale lock', proc.stdout)
        self.assertEqual(self.calls()[0], 'tokenatlas refresh --all')
        self.assertFalse(lock.exists())

    def test_old_live_verified_owner_is_never_taken_over(self):
        lock = self.state / 'collect.lock'
        pid, start = self.spawn_holder()
        self.write_owner(lock, pid, start)
        old = time.time() - 3 * 3600
        os.utime(lock / 'owner', (old, old))
        os.utime(lock, (old, old))
        proc = self.run_collect()
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f'lock held by live pid {pid} for 1', proc.stdout)
        self.assertNotIn('taking over', proc.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue((lock / 'owner').exists())

    def test_ownerless_young_lock_is_held(self):
        lock = self.state / 'collect.lock'
        lock.mkdir(parents=True)
        proc = self.run_collect()
        self.assertIn('already running', proc.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue(lock.exists())

    def test_ownerless_old_lock_is_taken_over(self):
        lock = self.state / 'collect.lock'
        lock.mkdir(parents=True)
        old = time.time() - 300
        os.utime(lock, (old, old))
        proc = self.run_collect()
        self.assertIn('taking over stale lock', proc.stdout)
        self.assertEqual(self.calls()[0], 'tokenatlas refresh --all')

    def test_release_only_removes_own_lock(self):
        # A step that replaces the owner file (as a takeover by another run would) keeps the lock on exit.
        lock = self.state / 'collect.lock'
        self.write_exe(self.root / 'bin' / 'tokenatlas',
                       '#!/bin/bash\nprintf "1\\nx\\nforeign\\n" > "$LOCK_DIR/owner"\nexit 0\n')
        proc = self.run_collect(LOCK_DIR=str(lock))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(lock.exists(), 'lock owned by someone else must survive our exit')

    def test_concurrent_runs_one_wins(self):
        lock = self.state / 'collect.lock'
        self.write_exe(self.root / 'bin' / 'tokenatlas',
                       '#!/bin/bash\necho "0 tokenatlas $*" >> "$CALLS"\nsleep 1\n')
        env = {'PATH': f'{self.root / "bin"}:/usr/bin:/bin', 'HOME': str(self.root / 'home'),
               'TOKENATLAS_STATE_DIR': str(self.state), 'CALLS': str(self.log)}
        procs = [subprocess.Popen(['bash', str(COLLECT)], env=env, stdout=subprocess.PIPE, text=True,
                                  start_new_session=True) for _ in range(6)]
        try:
            outs = [p.communicate(timeout=60)[0] for p in procs]
        finally:
            for p in procs:
                kill_group(p)
        self.assertEqual(sum('already running' not in o for o in outs), 1, outs)
        self.assertEqual(sum(c == 'tokenatlas refresh --all' for c in self.calls()), 1)
        self.assertFalse(lock.exists())


if __name__ == '__main__':
    unittest.main()
