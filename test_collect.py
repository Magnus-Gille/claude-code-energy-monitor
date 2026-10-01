"""tokenatlas collect: kernel lock, step order, remote-sync timeout, exit codes. Fakes only; no real host is contacted."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
POSIX = os.name != 'nt'

FAKE_SYNC = r'''#!/usr/bin/env bash
echo "sync $(python3 -c 'import time;print(time.time())') hosts=${REMOTE_HOSTS_OVERRIDE:-} report_exists=$([ -f "$STATE/report.html" ] && echo 1 || echo 0)" >> "$CALLS"
case "${FAKE_MODE:-ok}" in
  fail) echo "fake failure" >&2; exit 3;;
  hang) sleep 1000 & echo $! > "$CHILD_PID"; wait;;
esac
exit 0
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class CollectBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.state = self.tmp / 'state' / 'tokenatlas'
        self.state.mkdir(parents=True)
        self.calls = self.tmp / 'calls'
        self.child_pid = self.tmp / 'child.pid'
        self.sync = self.tmp / 'fake_sync.sh'
        self.sync.write_text(FAKE_SYNC)
        self.sync.chmod(0o755)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ('REMOTE_HOSTS_OVERRIDE', 'TOKENATLAS_REMOTE_SYNC')}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), XDG_STATE_HOME=str(self.tmp / 'state'),
                   STATE=str(self.state), CALLS=str(self.calls), CHILD_PID=str(self.child_pid),
                   PYTHONPATH=str(ROOT), **extra)
        return env

    def popen(self, *args, **extra):
        return subprocess.Popen([sys.executable, '-m', 'tokenatlas', 'collect', *args], env=self.env(**extra), cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def collect(self, *args, timeout=60, **extra):
        proc = self.popen(*args, **extra)
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def steps(self, out):
        """Step names in log order: 'refresh exit=0 (0.1s)' lines to ['refresh', ...]."""
        return re.findall(r'^\S+ collect: (.+?) exit=', out, re.M)

    def sync_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class CollectTest(CollectBase):
    def test_no_hosts_runs_refresh_and_report_only(self):
        proc = self.collect()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])
        self.assertTrue((self.state / 'report.html').exists())
        self.assertEqual(self.sync_calls(), [])
        self.assertTrue((self.state / 'collect.lock').exists(), 'the lock file is kept')

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_step_order_with_keep_text_and_sync(self):
        (self.state / 'top-prompts.json').write_text('{"version":2,"k":5,"by":"cost","entries":[]}\n')
        (self.state / 'top-prompts.json').chmod(0o600)
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'top', 'report', 'remote sync', 'report after sync'])
        calls = self.sync_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('hosts=pi:myhost', calls[0])
        self.assertIn('report_exists=1', calls[0], 'the first report was built before the sync')
        self.assertRegex(proc.stdout, r'refresh exit=0 \(\d+\.\ds\)')

    def test_top_only_when_opted_in(self):
        proc = self.collect()
        self.assertNotIn('top', self.steps(proc.stdout))

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_second_report_runs_when_sync_fails(self):
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync), FAKE_MODE='fail')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report', 'remote sync', 'report after sync'])
        self.assertIn('remote sync exit=3', proc.stdout)

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_no_report_skips_both_reports(self):
        proc = self.collect('--no-report', '--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'remote sync'])
        self.assertFalse((self.state / 'report.html').exists())

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_hosts_from_env_and_file(self):
        self.collect('--remote-sync', str(self.sync), REMOTE_HOSTS_OVERRIDE='a:h1 b:h2')
        self.assertIn('hosts=a:h1 b:h2', self.sync_calls()[0])
        self.calls.unlink()
        (self.state / 'remote-hosts').write_text('c:h3\nd:h4\n')
        self.collect('--remote-sync', str(self.sync))
        self.assertIn('hosts=c:h3 d:h4', self.sync_calls()[0])

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_remote_sync_env_override_and_packaged_default(self):
        self.collect('--remote', 'pi:myhost', TOKENATLAS_REMOTE_SYNC=str(self.sync))
        self.assertEqual(len(self.sync_calls()), 1)
        from tokenatlas import collect
        self.assertTrue(collect.PACKAGED_SYNC.is_file())

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_missing_sync_script_is_a_failure(self):
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.tmp / 'nope.sh'))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(self.steps(proc.stdout)[-1], 'report after sync')

    @unittest.skipUnless(POSIX, 'process groups are POSIX only')
    def test_sync_timeout_kills_process_group_and_still_reports(self):
        start = time.monotonic()
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync), '--sync-timeout', '2', FAKE_MODE='hang')
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('remote sync: timeout after 2s', proc.stdout)
        self.assertEqual(self.steps(proc.stdout)[-1], 'report after sync')
        pid = int(self.child_pid.read_text())
        time.sleep(0.3)
        self.assertFalse(alive(pid), 'the sleeping grandchild survived the timeout')


    @unittest.skipUnless(os.name == 'nt', 'Windows behaviour')
    def test_windows_skips_remote_sync_but_reports(self):
        proc = self.collect('--remote-sync', str(self.sync), REMOTE_HOSTS_OVERRIDE='a:h1')
        self.assertIn('remote sync skipped', proc.stdout)
        self.assertFalse(self.calls.exists() and self.sync_calls(), proc.stdout)


HOLDER = '''import fcntl,sys,time
f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('held',flush=True);time.sleep(1000)'''


@unittest.skipUnless(POSIX, 'flock holder is POSIX only')
class CollectLockTest(CollectBase):
    def hold(self):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.state / 'collect.lock')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def test_held_lock_skips_everything_and_exits_zero(self):
        self.hold()
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('collect: already running', proc.stdout)
        self.assertEqual(self.steps(proc.stdout), [])
        self.assertEqual(self.sync_calls(), [])
        self.assertFalse((self.state / 'report.html').exists())

    def test_lock_released_after_holder_is_killed(self):
        holder = self.hold()
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        proc = self.collect()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('already running', proc.stdout)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])

    def test_lock_released_after_normal_run(self):
        self.collect()
        proc = self.collect()
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])


if __name__ == '__main__':
    unittest.main()
