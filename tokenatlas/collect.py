"""tokenatlas collect: one scheduled run (refresh, top text, report, remote sync, report) under a kernel lock."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PACKAGED_SYNC=Path(__file__).with_name('remote_sync.sh')


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} collect: {msg}",flush=True)


def _lock(fd):
    """Take the exclusive non-blocking lock; False when another process holds it. The kernel drops it on exit or crash."""
    if os.name=='nt':
        import msvcrt
        try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        except OSError:return False
        return True
    import fcntl
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:return False
    return True


def _step(name,fn):
    """Run fn() -> exit code (an exception is a failure); log one line with exit status and timing; return the code."""
    start=time.monotonic()
    try:rc=fn()
    except SystemExit as exc:rc=exc.code if isinstance(exc.code,int) else 1
    except Exception as exc:
        log(f'{name}: {type(exc).__name__}: {exc}');rc=1
    log(f'{name} exit={rc} ({time.monotonic()-start:.1f}s)')
    return rc


def _quiet(call,*argv):
    """Run a CLI command in-process with its stdout/stderr kept off the log, except stderr lines on failure."""
    out,err=io.StringIO(),io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
        try:rc=call(list(argv))
        except SystemExit as exc:rc=exc.code if isinstance(exc.code,int) else 1
    if rc:
        for line in err.getvalue().splitlines()[-5:]:log(f'  {line}')
    return rc


def _hosts(args,state):
    if args.remote:return ' '.join(args.remote)
    if os.environ.get('REMOTE_HOSTS_OVERRIDE','').strip():return os.environ['REMOTE_HOSTS_OVERRIDE'].strip()
    try:return ' '.join((state/'remote-hosts').read_text().split())
    except OSError:return ''


def _sync(script,hosts,timeout):
    """Run the remote sync script in its own session; on timeout TERM the whole group, wait 2 s, then KILL it."""
    if not script.is_file():log(f'{script} not found');return 127
    proc=subprocess.Popen(['bash',str(script)],env={**os.environ,'REMOTE_HOSTS_OVERRIDE':hosts},start_new_session=True)
    try:return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f'remote sync: timeout after {timeout}s')
        for sig,wait in ((signal.SIGTERM,2),(signal.SIGKILL,None)):
            with contextlib.suppress(ProcessLookupError,PermissionError):os.killpg(proc.pid,sig)
            try:proc.wait(timeout=wait);break
            except subprocess.TimeoutExpired:pass
        with contextlib.suppress(ProcessLookupError,PermissionError):os.killpg(proc.pid,signal.SIGKILL)  # stragglers of an already exited leader
        return 124


def run(args):
    from tokenatlas import prompt_store
    from tokenatlas.__main__ import main,refresh_all
    from tokenatlas.history import History
    db=args.db.expanduser();state=db.parent
    try:state.mkdir(parents=True,exist_ok=True);lock=open(state/'collect.lock','a+')
    except OSError as exc:log(f'cannot open lock in {state}: {exc}');return 1
    with lock:
        if not _lock(lock.fileno()):log('already running');return 0
        def refresh():
            with History(db) as history:result=refresh_all(history)
            return 0 if result['status']=='ok' else 2
        def report(*extra):
            return lambda:_quiet(main,'--db',str(db),'report','--html',str(state/'report.html'),'--private','--if-changed','--lang',args.lang,*extra)
        failed=_step('refresh',refresh)!=0
        if prompt_store.store_path(db).exists():failed|=_step('top',lambda:_quiet(main,'--db',str(db),'top','--keep-text'))!=0
        if not args.no_report:failed|=_step('report',report('--max-age','1h'))!=0
        hosts=_hosts(args,state)
        if hosts:
            script=Path(args.remote_sync or os.environ.get('TOKENATLAS_REMOTE_SYNC') or PACKAGED_SYNC)
            if os.name=='nt':log('remote sync skipped: bash is not assumed on Windows')
            else:
                failed|=_step('remote sync',lambda:_sync(script,hosts,args.sync_timeout))!=0
                if not args.no_report:failed|=_step('report after sync',report())!=0  # partial imports show up even after a failed sync
        return int(failed)
