"""Versioned local history of observed usage, never a billing ledger.

Changed files are reparsed with the existing why readers. File checkpoints and
merged observations commit together; copying, retrying, or rotating a transcript
cannot delete history. Only token counters and attribution metadata are retained.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import why

VERSION = 1
COLLECTOR_VERSION = 3
FIELDS = ('fresh_input', 'cache_read', 'cache_write', 'output')
ALL_FIELDS = FIELDS + ('reasoning',)


def _owned_by_current_user(info):
    getuid = getattr(os, 'getuid', None)
    return not callable(getuid) or info.st_uid == getuid()


def _has_private_permissions(info):
    return os.name == 'nt' or stat.S_IMODE(info.st_mode) == 0o600


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def clean_usage(raw):
    """A second allowlist at the persistence boundary (no content or tool input)."""
    if not isinstance(raw, dict):
        return {}
    allowed = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
               'cache_creation_input_tokens', 'cached_input_tokens',
               'cache_write_input_tokens', 'reasoning_output_tokens', 'total_tokens',
               'input', 'output', 'reasoning', 'cacheRead', 'cacheWrite', 'totalTokens')
    result = {k: counter(raw[k]) for k in allowed if k in raw}
    for name, keys in (('cache_creation', ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')),
                       ('output_tokens_details', ('thinking_tokens',)),
                       ('cache', ('read', 'write'))):
        if isinstance(raw.get(name), dict):
            result[name] = {k: counter(raw[name][k]) for k in keys if k in raw[name]}
    if 'iterations' in raw:
        value = raw['iterations']
        result['iterations'] = None
        if isinstance(value, list):
            result['iterations'] = []
            for entry in value:
                if not isinstance(entry, dict):
                    result['iterations'].append({})
                    continue
                cleaned = clean_usage({k: v for k, v in entry.items() if k not in ('iterations', 'iteration_snapshots')})
                for key in ('type', 'model'):
                    if isinstance(entry.get(key), str):
                        cleaned[key] = entry[key]
                result['iterations'].append(cleaned)
    if isinstance(raw.get('iteration_snapshots'), list):
        result['iteration_snapshots'] = [clean_usage({'iterations': snapshot})['iterations']
            for snapshot in raw['iteration_snapshots'] if isinstance(snapshot, list)]
    return result


def normalize(record, machine):
    raw = clean_usage(getattr(record, 'raw_usage', {}))
    warnings = []
    if getattr(record, 'id_synthetic', False):
        warnings.append('synthetic_identity')
    if record.harness == 'claude':
        split = raw.get('cache_creation', {})
        writes = raw.get('cache_creation_input_tokens')
        parts = [split.get(k) for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')]
        if writes is None and all(v is not None for v in parts):
            writes = sum(parts)
        elif writes is not None and all(v is not None for v in parts) and sum(parts) != writes:
            warnings.append('cache_write_split_mismatch')
        tokens = dict(fresh_input=raw.get('input_tokens'), cache_read=raw.get('cache_read_input_tokens'),
                      cache_write=writes, output=raw.get('output_tokens'),
                      reasoning=raw.get('output_tokens_details', {}).get('thinking_tokens'))
    elif record.harness == 'codex':
        total = raw.get('input_tokens')
        read = raw.get('cached_input_tokens')
        write = raw.get('cache_write_input_tokens')
        fresh = total - read - write if all(x is not None for x in (total, read, write)) else None
        if fresh is not None and fresh < 0:
            fresh = None
            warnings.append('cache_exceeds_input')
        tokens = dict(fresh_input=fresh, cache_read=read, cache_write=write,
                      output=raw.get('output_tokens'), reasoning=raw.get('reasoning_output_tokens'))
    elif record.harness == 'pi':
        tokens = dict(fresh_input=raw.get('input'), cache_read=raw.get('cacheRead'),
                      cache_write=raw.get('cacheWrite'), output=raw.get('output'),
                      reasoning=raw.get('reasoning'))
    elif record.harness == 'opencode':
        output, reasoning = raw.get('output'), raw.get('reasoning')
        tokens = dict(fresh_input=raw.get('input'), cache_read=raw.get('cache', {}).get('read'),
                      cache_write=raw.get('cache', {}).get('write'),
                      output=output + reasoning if output is not None and reasoning is not None else None,
                      reasoning=reasoning)
    else:
        raise ValueError(f'unsupported harness {record.harness}')
    if any(tokens[k] is None for k in FIELDS):
        warnings.append('missing_token_fields')
    if tokens['reasoning'] is not None and tokens['output'] is not None and tokens['reasoning'] > tokens['output']:
        warnings.append('reasoning_exceeds_output')
    iterations = raw.get('iterations')
    if isinstance(iterations, list) and iterations:
        if any(not entry for entry in iterations):
            warnings.append('invalid_iteration')
        if len(iterations) != 1 or iterations[0].get('type') not in (None, 'message'):
            warnings.append('nontrivial_iterations')
        else:
            for field in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
                if iterations[0].get(field) is not None and raw.get(field) != iterations[0][field]:
                    warnings.append('iteration_parent_mismatch')
                    break
    snapshots = raw.get('iteration_snapshots', [])
    if any(not why._iteration_layout_compatible(a, b)
           for i, a in enumerate(snapshots) for b in snapshots[i + 1:]):
        warnings.append('iteration_layout_changed')
    project_id = getattr(record, 'project_id', '') or None
    return {
        'v': VERSION, 'kind': 'usage_observation', 'id': record.call_id,
        'id_synthetic': getattr(record, 'id_synthetic', False),
        'ts': record.timestamp.astimezone(timezone.utc).isoformat(), 'machine': machine,
        'harness': record.harness, 'provider': record.provider,
        'harness_version': getattr(record, 'harness_version', None),
        'collector': f'why-history@{COLLECTOR_VERSION}',
        'source_type': {'claude':'transcript','codex':'rollout','pi':'session','opencode':'database'}[record.harness],
        'session': record.session_id, 'parent_session': getattr(record, 'parent_session_id', None),
        'session_started_at': (getattr(record, 'session_started_at', None).astimezone(timezone.utc).isoformat()
                               if getattr(record, 'session_started_at', None) else None),
        'thread_kind': record.thread_kind, 'agent': record.agent, 'origin': record.entrypoint,
        'model': None if record.model == 'unknown' else record.model,
        'effort': None if record.effort == 'unknown' else record.effort,
        'project_id': project_id, 'project_label': record.project,
        'cwd': getattr(record, 'cwd', None), 'turn_id': getattr(record, 'turn_id', None),
        'turn_confidence': getattr(record, 'turn_confidence', 'absent'),
        'tokens': tokens, 'raw_usage': raw, 'duration_ms': None,
        'accounting_basis': 'request_top_level', 'billing_verified': False,
        'warnings': sorted(set(warnings)), 'complete': not warnings,
        'confidence': {k: 'absent' if v is None else 'observed' for k, v in tokens.items()},
    }


def merge_usage(a, b):
    return why._merge_sanitized_usage(a, b)


def merge_observations(a, b):
    """Max counters, stable metadata preference, then revalidate normalization."""
    # Lexical JSON tie-break makes equal-time metadata deterministic across imports.
    winner, other = sorted((a, b), key=lambda x: (x['ts'], json.dumps(x, sort_keys=True)), reverse=True)
    result = dict(winner)
    if result['harness'] == 'pi':
        starts = [(item.get('session_started_at'), item) for item in (a, b) if item.get('session_started_at')]
        if starts:
            _, original = min(starts, key=lambda pair: pair[0])
            for key in ('session', 'project_id', 'project_label', 'cwd', 'harness_version', 'session_started_at'):
                result[key] = original.get(key)
    for k, v in other.items():
        if result.get(k) in (None, '', 'unknown', 'absent') and v not in (None, '', 'unknown', 'absent'):
            result[k] = v
    raw = merge_usage(a['raw_usage'], b['raw_usage'])
    # Re-normalize coherent raw counters rather than merge derived fresh input.
    from types import SimpleNamespace
    proxy = SimpleNamespace(harness=result['harness'], provider=result['provider'],
        call_id=result['id'], timestamp=datetime.fromisoformat(result['ts']),
        session_id=result['session'], model=result['model'] or 'unknown', effort=result['effort'] or 'unknown',
        project=result['project_label'], project_id=result['project_id'], cwd=result['cwd'],
        entrypoint=result['origin'], thread_kind=result['thread_kind'], agent=result['agent'],
        parent_session_id=result['parent_session'], turn_id=result['turn_id'],
        turn_confidence=result['turn_confidence'], harness_version=result['harness_version'],
        session_started_at=(datetime.fromisoformat(result['session_started_at'])
                            if result.get('session_started_at') else None),
        raw_usage=raw, id_synthetic=result['id_synthetic'])
    return normalize(proxy, result['machine'])


class History:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self.connection = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('history database must not be a symlink')
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or not _owned_by_current_user(info):
                raise ValueError('history database must be a regular file owned by this user')
            if not _has_private_permissions(info):
                raise ValueError('history database permissions must be 0600')
        c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        self.connection = c
        try:
            c.execute('BEGIN IMMEDIATE')
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, VERSION):
                raise ValueError(f'unsupported history schema version {version}')
            for sql in (
                'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS observations (key TEXT PRIMARY KEY, data TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS sources (observation TEXT, harness TEXT, path TEXT, PRIMARY KEY(observation,harness,path))',
                'CREATE TABLE IF NOT EXISTS files (harness TEXT, path TEXT, root TEXT, fingerprint TEXT, diagnostics TEXT, PRIMARY KEY(harness,path))',
                'CREATE TABLE IF NOT EXISTS imports (harness TEXT, root TEXT, data TEXT, PRIMARY KEY(harness,root))',
            ):
                c.execute(sql)
            c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('machine', 'm-' + uuid.uuid4().hex))
            c.execute(f'PRAGMA user_version={VERSION}')
            c.commit()
        except Exception:
            c.rollback()
            c.close()
            raise
        self.machine = c.execute("SELECT value FROM meta WHERE key='machine'").fetchone()[0]
        return self

    def __exit__(self, *_):
        if self.connection:
            self.connection.close()

    @staticmethod
    def fingerprint(path, include_sqlite_sidecars=False):
        paths = [Path(path)]
        if include_sqlite_sidecars:
            paths.extend(Path(str(path) + suffix) for suffix in ('-wal', '-shm'))
        values = [COLLECTOR_VERSION]
        for candidate in paths:
            try:
                s = candidate.stat()
                values.append([candidate.name, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns])
            except FileNotFoundError:
                values.append([candidate.name, None])
        return json.dumps(values)

    def refresh(self, harness, root):
        if harness not in ('claude', 'codex', 'pi', 'opencode'):
            raise ValueError('unsupported harness')
        root = Path(root).expanduser().absolute()
        c = self.connection
        c.execute('BEGIN IMMEDIATE')
        try:
            prior = c.execute('SELECT data FROM imports WHERE harness=? AND root=?', (harness, str(root))).fetchone()
            prior = json.loads(prior[0]) if prior else {}
            result = dict(harness=harness, root=str(root), last_attempt=utcnow(),
                          last_success=prior.get('last_success'), status='ok', files_seen=0,
                          files_parsed=0, files_skipped=0, observations_seen=0,
                          malformed_lines=0, partial_lines=0, unparsed_usage_lines=0, read_errors=0, changed_during_read=0,
                          coverage_complete=False, errors=[])
            expects_file = harness == 'opencode'
            if not (root.is_file() if expects_file else root.is_dir()):
                result['status'] = 'missing'
                result['errors'].append('source database is not a readable file' if expects_file else
                                        'source root is not a readable directory')
                paths = []
            elif expects_file:
                paths = [root]
            else:
                pattern = {'claude':'*.jsonl', 'codex':'rollout-*.jsonl', 'pi':'*.jsonl'}[harness]
                def walk_error(exc):
                    result['read_errors'] += 1
                    result['errors'].append(f'{type(exc).__name__}: {exc}')
                paths = sorted(Path(directory) / name
                    for directory, _, names in os.walk(root, onerror=walk_error)
                    for name in names if Path(name).match(pattern))
            result['files_seen'] = len(paths)
            collect = {'claude':why.collect_claude, 'codex':why.collect_codex,
                       'pi':why.collect_pi, 'opencode':why.collect_opencode}[harness]
            for path in paths:
                try:
                    before = self.fingerprint(path, harness == 'opencode')
                    previous = c.execute('SELECT fingerprint,diagnostics FROM files WHERE harness=? AND path=?',
                                         (harness, str(path))).fetchone()
                    if previous and previous['fingerprint'] == before:
                        result['files_skipped'] += 1
                        diagnostics = json.loads(previous['diagnostics'])
                        for k in ('malformed_lines','partial_lines','unparsed_usage_lines'):
                            result[k] += diagnostics.get(k, 0)
                        continue
                    diagnostics = dict(malformed_lines=0, partial_lines=0, unparsed_usage_lines=0)
                    if harness != 'opencode':
                        with path.open('rb') as handle:
                            for raw in handle:
                                try:
                                    row = json.loads(raw)
                                    if not isinstance(row, dict):
                                        diagnostics['malformed_lines'] += 1
                                    else:
                                        message = why._mapping(row.get('message'))
                                        if harness in ('claude', 'pi'):
                                            raw_usage = message.get('usage')
                                            timestamp = why.parse_iso_timestamp(row.get('timestamp') or message.get('timestamp'))
                                        else:
                                            payload = why._mapping(row.get('payload'))
                                            raw_usage = why._mapping(payload.get('info')).get('last_token_usage') if payload.get('type') == 'token_count' else None
                                            timestamp = why.parse_iso_timestamp(row.get('timestamp'))
                                        if raw_usage is not None and (not isinstance(raw_usage, dict) or timestamp is None):
                                            diagnostics['unparsed_usage_lines'] += 1
                                except (ValueError, UnicodeError):
                                    diagnostics['malformed_lines' if raw.endswith(b'\n') else 'partial_lines'] += 1
                        records = collect(root, datetime(1970,1,1,tzinfo=timezone.utc),
                                          datetime(9999,1,1,tzinfo=timezone.utc), paths=[path], strict=True)
                    else:
                        try:
                            records = collect(path, datetime(1970,1,1,tzinfo=timezone.utc),
                                              datetime(9999,1,1,tzinfo=timezone.utc),
                                              diagnostics=diagnostics)
                        except sqlite3.Error as exc:
                            result['read_errors'] += 1
                            result['errors'].append(f'{path}: {type(exc).__name__}: {exc}')
                            continue
                    result['files_parsed'] += 1
                    changed = before != self.fingerprint(path, harness == 'opencode')
                    if changed:
                        result['changed_during_read'] += 1
                        if harness != 'opencode':
                            continue
                    for record in records:
                        item = normalize(record, self.machine)
                        # Synthetic IDs are machine scoped; provider IDs can merge copies.
                        key = json.dumps([item['provider'], item['harness'],
                                          self.machine if item['id_synthetic'] else '', item['id']])
                        old = c.execute('SELECT data FROM observations WHERE key=?', (key,)).fetchone()
                        if old:
                            item = merge_observations(json.loads(old[0]), item)
                        c.execute('INSERT OR REPLACE INTO observations VALUES (?,?)',
                                  (key, json.dumps(item, sort_keys=True)))
                        c.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?)', (key,harness,str(path)))
                        result['observations_seen'] += 1
                    for k in diagnostics:
                        result[k] += diagnostics.get(k, 0)
                    # Never checkpoint an incomplete tail; try it again next refresh.
                    checkpoint = before if not diagnostics['partial_lines'] and not changed else None
                    c.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                              (harness,str(path),str(root),checkpoint,json.dumps(diagnostics)))
                except (OSError, UnicodeError) as exc:
                    result['read_errors'] += 1
                    result['errors'].append(f'{path}: {type(exc).__name__}: {exc}')
            if result['status'] == 'ok':
                if any(result[k] for k in ('malformed_lines','partial_lines','unparsed_usage_lines','read_errors','changed_during_read')):
                    result['status'] = 'partial'
                else:
                    result['last_success'] = utcnow()
            c.execute('INSERT OR REPLACE INTO imports VALUES (?,?,?)', (harness,str(root),json.dumps(result)))
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise

    def records(self, start=None, end=None, harness=None, project=None, session=None, turn=None):
        session_harness = None
        raw_session = session
        if isinstance(session, str):
            prefix, separator, candidate = session.partition(':')
            if separator and prefix in ('claude','codex','pi','opencode'):
                session_harness, raw_session = prefix, candidate
        if harness is not None and session_harness is not None and harness != session_harness:
            return []
        effective_harness = harness or session_harness
        result = []
        for row in self.connection.execute('SELECT key,data FROM observations'):
            item = json.loads(row['data'])
            ts = datetime.fromisoformat(item['ts'])
            if start is not None and ts < start or end is not None and ts >= end:
                continue
            if any(wanted is not None and item[key] != wanted for key, wanted in (
                ('harness',effective_harness),('project_id',project),('session',raw_session),('turn_id',turn))):
                continue
            item['sources'] = [r[0] for r in self.connection.execute(
                'SELECT path FROM sources WHERE observation=? ORDER BY path',(row['key'],))]
            result.append(item)
        return sorted(result,key=lambda x:(x['ts'],x['provider'],x['id']))

    def doctor(self):
        records = self.records()
        imports = [json.loads(r[0]) for r in self.connection.execute('SELECT data FROM imports ORDER BY harness,root')]
        missing = sum(not Path(r[0]).exists() for r in self.connection.execute('SELECT path FROM files'))
        return dict(schema_version=VERSION, machine=self.machine, observations=len(records),
                    first_event=records[0]['ts'] if records else None,
                    last_event=records[-1]['ts'] if records else None,
                    missing_source_files=missing, imports=imports, coverage_complete=False,
                    incomplete_observations=sum(not x['complete'] for x in records),
                    unlinked_turns=sum(x['turn_id'] is None for x in records),
                    billing_verified=False,
                    notes=['Local retained sources only; missing history cannot be reconstructed.',
                           'Request observations are not verified billable inference passes.'])


def summarize(records, granularity='day', timezone_name='UTC'):
    zone = ZoneInfo(timezone_name)
    if granularity not in ('day','hour','minute'):
        raise ValueError('granularity must be day, hour or minute')
    def aggregate(rows):
        identified = [r for r in rows if not r['id_synthetic']]
        ambiguous = [r for r in rows if r['id_synthetic']]
        known = {k:sum(r['tokens'][k] or 0 for r in identified) for k in ALL_FIELDS}
        ambiguous_fields = {k:sum(r['tokens'][k] or 0 for r in ambiguous) for k in ALL_FIELDS}
        missing = {k:sum(r['tokens'][k] is None for r in rows) for k in ALL_FIELDS}
        complete = bool(rows) and all(r['complete'] for r in rows)
        return dict(observations=len(rows), known_tokens=sum(known[k] for k in FIELDS),
                    tokens=sum(known[k] for k in FIELDS) if complete else None,
                    token_fields={k:known[k] if rows and not missing[k] and not ambiguous else None for k in ALL_FIELDS},
                    ambiguous_identity_observations=len(ambiguous),
                    ambiguous_identity_tokens=sum(ambiguous_fields[k] for k in FIELDS),
                    ambiguous_identity_token_fields=ambiguous_fields,
                    known_token_fields=known, missing_fields=missing, complete=complete)
    buckets = {}
    for record in records:
        dt = datetime.fromisoformat(record['ts']).astimezone(zone)
        if granularity == 'day':
            label = dt.date().isoformat()
        elif granularity == 'hour':
            label = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        else:
            label = dt.replace(second=0, microsecond=0).isoformat()
        buckets.setdefault(label,[]).append(record)
    groups = {}
    for key in ('harness','provider','origin','project_id','session','turn_id','model','effort','thread_kind'):
        entries = {}
        for record in records:
            name = record[key] or 'unknown'
            if key == 'session' and name != 'unknown':
                name = f"{record['harness']}:{name}"
            entries.setdefault(name, []).append(record)
        groups[key] = [dict(name=name, **aggregate(rows)) for name, rows in sorted(
            entries.items(),key=lambda pair:(-aggregate(pair[1])['known_tokens'],pair[0]))]
    return dict(timezone=timezone_name, granularity=granularity, totals=aggregate(records),
                buckets=[dict(time=label,**aggregate(rows)) for label,rows in sorted(buckets.items())],
                groups=groups, coverage_complete=False, billing_verified=False)
