"""tokenatlas statusline: the Claude Code statusline as a packaged command, plus the small cache `refresh` writes for it.

Claude Code runs the command on every status update, so this module stays light (stdlib and tokenatlas.energy only; the history is never
imported or opened). Day, week and month totals come from statusline.json next to the history database, written by refresh; context and
quota are live from the payload on stdin. No network, no credentials, nothing is written by the statusline itself.
"""
import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import energy

CACHE_NAME = 'statusline.json'
CACHE_DAYS = 31  # per-local-day buckets kept in the cache, today included
STALE_SECONDS = 45 * 60
CLASSES = ('fresh_input', 'cache_read', 'cache_write', 'output')
COUNTERS = CLASSES + ('mwh', 'requests', 'unweighted', 'incomplete')


def state_dir():
    """Default history directory, read-only (the one-time agentmon move stays with the history commands)."""
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'tokenatlas'


def cache_path(db):
    return Path(db).expanduser().absolute().with_name(CACHE_NAME)


def build_cache(history, now=None):
    """Per-local-day buckets for the last 31 days from one query over ~32 days (ambiguous observations excluded), with the revision."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    first = today - timedelta(days=CACHE_DAYS - 1)
    since = datetime.combine(first - timedelta(days=1), datetime.min.time()).astimezone()  # one day of slack for the zone offset
    days = {}
    c = history.connection
    began = not c.in_transaction
    if began:c.execute('BEGIN')
    try:
        revision = history.revision
        rows = c.execute('SELECT o.ts_us,p.value,m.value,o.fresh_input,o.cache_read,o.cache_write,o.output,o.complete,o.reasoning,q.value FROM observations o'
                         ' LEFT JOIN strings p ON p.id=o.provider LEFT JOIN strings m ON m.id=o.model LEFT JOIN strings q ON q.id=o.quota'
                         ' WHERE o.ts_us>=? AND COALESCE(o.id_synthetic,0)=0', (int(since.timestamp()) * 1_000_000,)).fetchall()
    finally:
        if began:c.rollback()
    for ts_us, provider, model, *tokens, complete, reasoning, quota in rows:
        if quota and not any(tokens) and not reasoning and (json.loads(quota) or {}).get('status') in ('rejected', 'event'):continue  # a limit event (history.is_limit_event) is no request
        day = datetime.fromtimestamp(ts_us // 1_000_000).date()  # the machine's local zone
        if not first <= day <= today:continue
        bucket = days.setdefault(day.isoformat(), dict.fromkeys(COUNTERS, 0))
        counts = dict(zip(CLASSES, (t or 0 for t in tokens)))
        mult, weighted = energy.multiplier(provider, model)
        for k, v in counts.items():bucket[k] += v
        bucket['mwh'] += energy.mid_mwh(counts, mult)
        bucket['requests'] += 1
        bucket['unweighted'] += not weighted
        bucket['incomplete'] += not complete
    return dict(v=1, written_at=now.astimezone(timezone.utc).isoformat(timespec='seconds'), revision=revision, days=days)


def write_atomic(path, payload):
    """Write JSON next to its destination with mode 0600 and rename it into place."""
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')  # mkstemp creates the file 0600
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        try:os.unlink(temp)
        except OSError:pass
        raise


def refresh_cache(history):
    """Write statusline.json beside the history; a failure is a warning on stderr and never fails the refresh."""
    try:
        write_atomic(cache_path(history.path), build_cache(history))
    except Exception as exc:
        print(f'warning: could not write {CACHE_NAME}: {type(exc).__name__}: {exc}', file=sys.stderr)


def tokens_text(n):
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if n >= scale:return f'{n / scale:.1f}{suffix}' if suffix != 'K' else f'{n / scale:.0f}K'
    return str(int(n))


def window_totals(days, today, length):
    """(tokens, mwh) over the last `length` local days ending today."""
    tokens = mwh = 0
    for i in range(length):
        bucket = days.get((today - timedelta(days=i)).isoformat())
        if bucket:
            tokens += sum(bucket[k] for k in CLASSES)
            mwh += bucket['mwh']
    return tokens, mwh


def totals_segments(cache, now):
    """['D:2.0M ~2 kWh', 'W:...', 'M:...'] with the cache time appended when it is older than 45 minutes."""
    today = now.astimezone().date()
    parts = [f'{label}:{tokens_text(t)} {energy.fmt(e)}'.removesuffix(' 0 mWh') if t == 0 else f'{label}:{tokens_text(t)} {energy.fmt(e)}'
             for label, length in (('D', 1), ('W', 7), ('M', 30)) for t, e in [window_totals(cache['days'], today, length)]]
    written = datetime.fromisoformat(cache['written_at'])
    if (now - written).total_seconds() > STALE_SECONDS:
        parts[-1] += f" ({written.astimezone().strftime('%H:%M')})"
    return parts


def _percent(value):
    """A finite number as a whole percent, else None (a missing, non-numeric, NaN or infinite value is left out)."""
    return f'{value:.0f}%' if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def render(payload, cache, now):
    """The one status line; a missing context or quota is omitted, a missing or unreadable cache omits the totals."""
    model = (payload.get('model') or {}).get('display_name') or '?'
    parts = [model]
    ctx = _percent((payload.get('context_window') or {}).get('used_percentage'))
    if ctx:parts.append(f'Ctx:{ctx}')
    limits = payload.get('rate_limits') or {}
    q5, q7 = (_percent((limits.get(k) or {}).get('used_percentage')) for k in ('five_hour', 'seven_day'))
    quota = [f'{label}:{q}' for label, q in (('5h', q5), ('7d', q7)) if q]
    if quota:parts.append(' '.join(quota))
    try:
        parts += totals_segments(cache, now)
    except Exception:
        pass  # no totals rather than a failed status line
    return ' | '.join(parts)


def read_cache(path):
    try:
        cache = json.loads(Path(path).read_text(encoding='utf-8'))
        return cache if isinstance(cache, dict) and isinstance(cache.get('days'), dict) else None
    except (OSError, ValueError):
        return None


def executable():
    """Absolute command that runs this tokenatlas: the running executable, else the one on PATH, else this interpreter with -m."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name.lower() in ('tokenatlas', 'tokenatlas.exe') and argv0.exists():
        return str(argv0.resolve())
    found = shutil.which('tokenatlas')
    return str(Path(found).resolve()) if found else f'{sys.executable} -m tokenatlas'


WINDOWS_UNSAFE = re.compile(r'[^\w\-.:\\/ ]')  # cmd.exe has no quoting that is safe for &, |, ^, %, ! and the like


def _quote(arg, windows=None):
    """One argument quoted for the shell that runs the statusLine command: POSIX quoting, or double quotes on Windows (valid in cmd.exe and bash)."""
    if not (os.name == 'nt' if windows is None else windows):
        return shlex.quote(arg)
    return subprocess.list2cmdline([arg])


def setup_text(db=None, command=None, windows=None):
    """The statusLine snippet for Claude Code's settings.json and where that file is; the file itself is never touched."""
    config = os.environ.get('CLAUDE_CONFIG_DIR')
    settings = (Path(config).expanduser() if config else Path.home() / '.claude') / 'settings.json'
    command = command or executable()
    windows = os.name == 'nt' if windows is None else windows
    args = ([] if ' -m ' in command else [command]) + ([str(Path(db).expanduser().absolute())] if db is not None else [])
    unsafe = windows and any(WINDOWS_UNSAFE.search(a) for a in args)
    if ' -m ' not in command:command = _quote(command, windows)
    if db is not None:command += f' --db {_quote(str(Path(db).expanduser().absolute()), windows)}'
    snippet = json.dumps({'statusLine': {'type': 'command', 'command': f'{command} statusline'}}, indent=2)
    warning = ('Warning: a path in this command contains a character that cmd.exe treats specially (&, |, ^, %, ! ...) and that no quoting makes '
               'safe; install TokenAtlas (and the database) under a plain path, or check that the command works before relying on it.\n\n') if unsafe else ''
    return (f'{warning}Add this to {settings} (merge it into the existing JSON; this command never edits the file):\n\n{snippet}\n\n'
            'Totals refresh whenever tokenatlas refresh, open or collect runs; context and quota are live.')


def run(argv, db=None, stdin=None, now=None):
    """Entry point: print one line and return 0, whatever happens; it writes nothing."""
    parser = argparse.ArgumentParser(prog='tokenatlas statusline', description='Claude Code statusline: reads its JSON payload on stdin and the cache refresh writes; no network.')
    parser.add_argument('--setup', action='store_true', help="Print the statusLine snippet for Claude Code's settings and its location, without editing it.")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help exits 0 after printing; a bad option must not break the status bar: fallback line, exit 0
        if exc.code not in (0, None):print('TokenAtlas')
        return 0
    if args.setup:
        print(setup_text(db))
        return 0
    model = None
    try:
        payload = json.loads((stdin or sys.stdin).read(), parse_constant=lambda name: None)  # NaN/Infinity are missing values
        model = (payload.get('model') or {}).get('display_name') if isinstance(payload, dict) else None
        cache = read_cache(cache_path(db if db is not None else state_dir() / 'history.sqlite3'))
        print(render(payload, cache, now or datetime.now(timezone.utc)))
    except Exception:
        print(model if isinstance(model, str) and model else 'TokenAtlas')
    return 0
