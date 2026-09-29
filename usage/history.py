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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import why

OBSERVATION_VERSION = 1  # the 'v' field inside observation dicts
SCHEMA_VERSION = 2  # PRAGMA user_version of the SQLite layout
COLLECTOR_VERSION = 5
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
               'total_input_tokens', 'total_output_tokens',
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
                    if (text := why._meta_text(entry.get(key))) is not None:
                        cleaned[key] = text
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
        if output is not None and reasoning is not None:
            output += reasoning
        tokens = dict(fresh_input=raw.get('input'), cache_read=raw.get('cache', {}).get('read'),
                      cache_write=raw.get('cache', {}).get('write'), output=output,
                      reasoning=reasoning)
    else:
        raise ValueError(f'unsupported harness {record.harness}')
    output_final = getattr(record, 'output_final', None)
    output_final = output_final if isinstance(output_final, bool) else None
    if record.harness == 'claude' and output_final is False:
        warnings.append('output_not_final')
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
    text = why._meta_text
    model, effort = text(record.model, default='unknown'), text(record.effort, default='unknown')
    version = getattr(record, 'harness_version', None)
    version = str(version) if isinstance(version, int) and not isinstance(version, bool) else text(version, limit=64)
    tariff = getattr(record, 'tariff', None)
    tariff = {k: v for k in ('speed', 'service_tier', 'inference_geo')
              if (v := text((tariff or {}).get(k), limit=64)) is not None} or None
    confidence = {k: 'absent' if v is None else 'observed' for k, v in tokens.items()}
    if 'output_not_final' in warnings:
        for k in ('output', 'reasoning'):
            if tokens[k] is not None:
                confidence[k] = 'lower_bound'
    return {
        'v': OBSERVATION_VERSION, 'kind': 'usage_observation', 'id': record.call_id,
        'id_synthetic': getattr(record, 'id_synthetic', False),
        'ts': record.timestamp.astimezone(timezone.utc).isoformat(), 'machine': machine,
        'harness': record.harness, 'provider': text(record.provider, default='unknown'),
        'harness_version': version,
        'collector': f'why-history@{COLLECTOR_VERSION}',
        'source_type': {'claude':'transcript','codex':'rollout','pi':'session','opencode':'database'}[record.harness],
        'session': text(record.session_id, default='unknown'),
        'parent_session': text(getattr(record, 'parent_session_id', None)),
        'session_started_at': (getattr(record, 'session_started_at', None).astimezone(timezone.utc).isoformat()
                               if getattr(record, 'session_started_at', None) else None),
        'thread_kind': text(record.thread_kind, default='unknown'),
        'agent': text(record.agent, default='unknown'), 'origin': text(record.entrypoint, default='unknown'),
        'model': None if model == 'unknown' else model, 'effort': None if effort == 'unknown' else effort,
        'project_id': project_id, 'project_label': record.project,
        'cwd': text(getattr(record, 'cwd', None), limit=4096),
        'turn_id': text(getattr(record, 'turn_id', None)),
        'turn_confidence': getattr(record, 'turn_confidence', 'absent'), 'tariff': tariff,
        'tokens': tokens, 'raw_usage': raw, 'duration_ms': None,
        'accounting_basis': 'request_top_level', 'billing_verified': False,
        'warnings': sorted(set(warnings)), 'complete': not warnings,
        'confidence': confidence, 'output_final': output_final,
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
    if result['harness'] == 'claude' and a['session'] != b['session']:
        # The earliest copy owns a request; on equal time keep the stored one (a).
        owner = b if b['ts'] < a['ts'] else a
        result['session'], result['parent_session'] = owner['session'], owner['parent_session']
    result['tariff'] = {**(other.get('tariff') or {}), **(winner.get('tariff') or {})} or None
    flags = [x.get('output_final') for x in (a, b)]
    output_final = True if True in flags else False if False in flags else None
    raw = merge_usage(a['raw_usage'], b['raw_usage'])
    # Re-normalize coherent raw counters rather than merge derived fresh input.
    from types import SimpleNamespace
    proxy = SimpleNamespace(harness=result['harness'], provider=result['provider'],
        call_id=result['id'], timestamp=datetime.fromisoformat(result['ts']),
        session_id=result['session'], model=result['model'] or 'unknown', effort=result['effort'] or 'unknown',
        project=result['project_label'], project_id=result['project_id'], cwd=result['cwd'],
        entrypoint=result['origin'], thread_kind=result['thread_kind'], agent=result['agent'],
        parent_session_id=result['parent_session'], turn_id=result['turn_id'],
        turn_confidence=result['turn_confidence'], tariff=result.get('tariff'), harness_version=result['harness_version'],
        session_started_at=(datetime.fromisoformat(result['session_started_at'])
                            if result.get('session_started_at') else None),
        raw_usage=raw, id_synthetic=result['id_synthetic'], output_final=output_final)
    return normalize(proxy, result['machine'])


# ---- Storage codec: one observation dict <-> one typed row -------------------------------------------
# Text and small-JSON fields become references into the global `strings` dictionary; _encode/_decode
# work on logical rows (values, not ids) and History maps reference columns to ids and back.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICRO = timedelta(microseconds=1)
_REFS = ('id', 'machine', 'harness', 'provider', 'harness_version', 'collector', 'source_type', 'session',
         'parent_session', 'session_started_at', 'thread_kind', 'agent', 'origin', 'model', 'effort',
         'project_id', 'project_label', 'cwd', 'turn_id', 'turn_confidence', 'tariff', 'warnings', 'confidence')
_JSON_REFS = ('tariff', 'warnings', 'confidence')
_FLAGS = ('id_synthetic', 'complete', 'output_final')
_TOKENS = ALL_FIELDS
_CONSTANTS = (('v', OBSERVATION_VERSION), ('kind', 'usage_observation'), ('duration_ms', None),
              ('accounting_basis', 'request_top_level'), ('billing_verified', False))
_ITEM_KEYS = frozenset(_REFS + _FLAGS + ('ts', 'tokens', 'raw_usage') + tuple(k for k, _ in _CONSTANTS))
_COLUMN_OF = {'id': 'call_id'}
COLUMNS = (('key', 'ts_us') + tuple(_COLUMN_OF.get(k, k) for k in _REFS) + _FLAGS + _TOKENS
           + ('raw_usage', 'extra'))
_REF_INDEX = tuple(range(2, 2 + len(_REFS)))
# Fixed raw_usage layout: (top-level key, None) or (container, member). Codex counters come first so that
# the positional array stays short; -1 marks an absent slot (counters are never negative), null is None.
_RAW_LAYOUT = tuple((k, None) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens',
    'reasoning_output_tokens', 'total_tokens', 'cache_write_input_tokens', 'cache_read_input_tokens',
    'cache_creation_input_tokens', 'total_input_tokens', 'total_output_tokens', 'input', 'output',
    'reasoning', 'cacheRead', 'cacheWrite', 'totalTokens')) + (
    ('cache_creation', 'ephemeral_5m_input_tokens'), ('cache_creation', 'ephemeral_1h_input_tokens'),
    ('output_tokens_details', 'thinking_tokens'), ('cache', 'read'), ('cache', 'write'))
_RAW_SLOT = {slot: i for i, slot in enumerate(_RAW_LAYOUT)}
_RAW_MEMBERS = {}
for _name, _member in _RAW_LAYOUT:
    if _member is not None:
        _RAW_MEMBERS.setdefault(_name, set()).add(_member)
_ABSENT = -1
_OBSERVATIONS_DDL = ('CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, '
                     'ts_us INTEGER NOT NULL, ' + ', '.join(
                         f'{name} {"TEXT" if name in ("raw_usage", "extra") else "INTEGER"}'
                         for name in COLUMNS[2:]) + ')')


def _counter_or_none(value):
    return value is None or (type(value) is int and value >= 0)


def _compact(value):
    return json.dumps(value, separators=(',', ':'))


def _pack_raw(raw):
    """Return (positional counter array JSON or None, JSON of everything outside the layout or None)."""
    slots, extra = [_ABSENT] * len(_RAW_LAYOUT), {}
    for key, value in raw.items():
        if key in _RAW_MEMBERS:
            if (isinstance(value, dict) and value and set(value) <= _RAW_MEMBERS[key]
                    and all(_counter_or_none(v) for v in value.values())):
                for member, v in value.items():
                    slots[_RAW_SLOT[key, member]] = v
                continue
        elif (key, None) in _RAW_SLOT and _counter_or_none(value):
            slots[_RAW_SLOT[key, None]] = value
            continue
        extra[key] = value
    while slots and slots[-1] == _ABSENT:
        slots.pop()
    return (_compact(slots) if slots else None), (_compact(extra) if extra else None)


def _unpack_raw(packed, extra):
    raw = {}
    for (name, member), value in zip(_RAW_LAYOUT, json.loads(packed) if packed else ()):
        if value != _ABSENT:
            if member is None:
                raw[name] = value
            else:
                raw.setdefault(name, {})[member] = value
    if extra:
        raw.update(json.loads(extra))
    return raw


def _ts_us(text):
    micros = (datetime.fromisoformat(text) - _EPOCH) // _MICRO
    if _ts_text(micros) != text:
        raise ValueError(f'timestamp {text!r} is not a canonical UTC ISO string')
    return micros


def _ts_text(micros):
    return (_EPOCH + timedelta(microseconds=micros)).isoformat()


def _key(item):
    return json.dumps([item['provider'], item['harness'], item['machine'] if item['id_synthetic'] else '', item['id']])


def _encode(item, key=None):
    """Observation dict -> logical row in COLUMNS order (reference columns hold strings, not ids)."""
    unknown = set(item) - _ITEM_KEYS - {'sources'}
    if unknown:
        raise ValueError(f'unstorable observation fields {sorted(unknown)}')
    for name, constant in _CONSTANTS:
        if type(item.get(name, constant)) is not type(constant) or item.get(name, constant) != constant:
            raise ValueError(f'observation field {name} must be {constant!r}')
    refs = []
    for name in _REFS:
        value = item.get(name)
        if name in _JSON_REFS:
            value = None if name == 'tariff' and value is None else _compact(value)
        elif value is not None and type(value) is not str:
            raise ValueError(f'observation field {name} must be text or null')
        refs.append(value)
    flags = []
    for name in _FLAGS:
        value = item.get(name)
        if value is not None and type(value) is not bool or value is None and name != 'output_final':
            raise ValueError(f'observation field {name} must be boolean')
        flags.append(None if value is None else int(value))
    tokens = item.get('tokens') or {}
    if set(tokens) - set(_TOKENS) or not all(_counter_or_none(tokens.get(k)) for k in _TOKENS):
        raise ValueError('observation tokens must be non-negative integers or null')
    packed, extra = _pack_raw(item.get('raw_usage') or {})
    return (_key(item) if key is None else key, _ts_us(item['ts']), *refs, *flags,
            *(tokens.get(k) for k in _TOKENS), packed, extra)


def _decode(row):
    """Inverse of _encode; returns the observation dict exactly as normalize produced it."""
    key, ts_us, *rest = row
    refs = dict(zip(_REFS, rest))
    flags = dict(zip(_FLAGS, rest[len(_REFS):]))
    n = len(_REFS) + len(_FLAGS)
    tokens = dict(zip(_TOKENS, rest[n:n + len(_TOKENS)]))
    packed, extra = rest[n + len(_TOKENS):]
    item = {'v': OBSERVATION_VERSION, 'kind': 'usage_observation', 'id': refs['id'],
            'id_synthetic': bool(flags['id_synthetic']), 'ts': _ts_text(ts_us)}
    for name in _REFS[1:-3]:
        item[name] = refs[name]
    item['tariff'] = None if refs['tariff'] is None else json.loads(refs['tariff'])
    item.update(tokens=tokens, raw_usage=_unpack_raw(packed, extra), duration_ms=None,
                accounting_basis='request_top_level', billing_verified=False,
                warnings=json.loads(refs['warnings']), complete=bool(flags['complete']),
                confidence=json.loads(refs['confidence']),
                output_final=None if flags['output_final'] is None else bool(flags['output_final']))
    return item


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
        self._strings, self._values = {}, {}
        try:
            c.execute('BEGIN IMMEDIATE')
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError(f'unsupported history schema version {version}')
            migrate = version == 1 and any(r['name'] == 'data' for r in c.execute('PRAGMA table_info(observations)'))
            if migrate:
                for table in ('observations', 'sources', 'files'):
                    c.execute(f'ALTER TABLE {table} RENAME TO v1_{table}')
            for sql in (
                'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE)',
                'CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, harness TEXT NOT NULL, path TEXT NOT NULL,'
                ' root TEXT, fingerprint TEXT, diagnostics TEXT, UNIQUE(harness, path))',
                _OBSERVATIONS_DDL,
                'CREATE TABLE IF NOT EXISTS sources (observation INTEGER, file INTEGER,'
                ' PRIMARY KEY(observation, file)) WITHOUT ROWID',
                'CREATE TABLE IF NOT EXISTS imports (harness TEXT, root TEXT, data TEXT, PRIMARY KEY(harness,root))',
            ):
                c.execute(sql)
            if not any(r['name'] == 'tariff' for r in c.execute('PRAGMA table_info(observations)')):
                c.execute('ALTER TABLE observations ADD COLUMN tariff INTEGER')  # additive within schema 2
            if migrate:
                self._migrate_v1(c)
            c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('machine', 'm-' + uuid.uuid4().hex))
            c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            c.commit()
            if migrate:
                c.execute('VACUUM')  # outside the transaction; reclaims the space of the dropped v1 tables
        except Exception:
            if c.in_transaction:
                c.rollback()
            c.close()
            raise
        self.machine = c.execute("SELECT value FROM meta WHERE key='machine'").fetchone()[0]
        return self

    def _migrate_v1(self, c):
        """Move JSON observations and text-keyed sources into schema v2, then drop the v1 tables."""
        c.execute('INSERT INTO files(harness,path,root,fingerprint,diagnostics)'
                  ' SELECT harness,path,root,fingerprint,diagnostics FROM v1_files')
        # A retained observation may cite a file that has no checkpoint row; keep the citation.
        c.execute('INSERT OR IGNORE INTO files(harness,path) SELECT DISTINCT harness,path FROM v1_sources')
        for row in c.execute('SELECT key,data FROM v1_observations'):
            try:
                encoded = _encode(json.loads(row['data']), row['key'])
            except ValueError as exc:
                raise ValueError(f"cannot migrate observation {row['key']}: {exc}") from exc
            self._insert(c, encoded)
        for row in c.execute('SELECT observation,harness,path FROM v1_sources').fetchall():
            found = c.execute('SELECT o.id,f.id FROM observations o, files f WHERE o.key=? AND f.harness=? AND f.path=?',
                              (self._physical_key(c, row['observation']), row['harness'], row['path'])).fetchone()
            if found:
                c.execute('INSERT OR IGNORE INTO sources VALUES (?,?)', tuple(found))
        for table in ('sources', 'files', 'observations'):
            c.execute(f'DROP TABLE v1_{table}')

    def _sid(self, c, value):
        if value is None:
            return None
        found = self._strings.get(value)
        if found is None:
            row = c.execute('SELECT id FROM strings WHERE value=?', (value,)).fetchone()
            found = row[0] if row else c.execute('INSERT INTO strings(value) VALUES (?)', (value,)).lastrowid
            self._strings[value] = found
            self._values[found] = value
        return found

    def _physical_key(self, c, logical):
        """Compact unique key: the dictionary ids of (provider, harness, machine scope, id), dot-joined."""
        return '.'.join(str(self._sid(c, part)) for part in json.loads(logical))

    def _insert(self, c, encoded):
        """Upsert by key, keeping the observation's integer id (and thus its sources) stable; returns that id."""
        row = [self._physical_key(c, encoded[0])] + [
            v if i not in _REF_INDEX else self._sid(c, v) for i, v in enumerate(encoded) if i]
        updates = ','.join(f'{name}=excluded.{name}' for name in COLUMNS[1:])
        c.execute(f'INSERT INTO observations({",".join(COLUMNS)}) VALUES ({",".join("?" * len(COLUMNS))})'
                  f' ON CONFLICT(key) DO UPDATE SET {updates}', row)
        return c.execute('SELECT id FROM observations WHERE key=?', (row[0],)).fetchone()[0]

    def _value(self, c, ident):
        if ident not in self._values:
            self._values[ident] = c.execute('SELECT value FROM strings WHERE id=?', (ident,)).fetchone()[0]
        return self._values[ident]

    def _existing(self, c, logical_key):
        row = c.execute(f'SELECT {",".join(COLUMNS)} FROM observations WHERE key=?',
                        (self._physical_key(c, logical_key),)).fetchone()
        return row and _decode(tuple(self._value(c, v) if i in _REF_INDEX and v is not None else v
                                     for i, v in enumerate(row)))

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
        self._strings, self._values = {}, {}
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
                    file_id = self._file_id(c, harness, str(path))
                    for record in records:
                        item = normalize(record, self.machine)
                        # Synthetic IDs are machine scoped; provider IDs can merge copies.
                        key = _key(item)
                        old = self._existing(c, key)
                        if old:
                            item = merge_observations(old, item)
                        c.execute('INSERT OR IGNORE INTO sources VALUES (?,?)',
                                  (self._insert(c, _encode(item, key)), file_id))
                        result['observations_seen'] += 1
                    for k in diagnostics:
                        result[k] += diagnostics.get(k, 0)
                    # Never checkpoint an incomplete tail; try it again next refresh.
                    checkpoint = before if not diagnostics['partial_lines'] and not changed else None
                    c.execute('UPDATE files SET root=?,fingerprint=?,diagnostics=? WHERE id=?',
                              (str(root),checkpoint,json.dumps(diagnostics),file_id))
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
            self._strings, self._values = {}, {}
            raise

    @staticmethod
    def _file_id(c, harness, path):
        c.execute('INSERT OR IGNORE INTO files(harness,path) VALUES (?,?)', (harness, path))
        return c.execute('SELECT id FROM files WHERE harness=? AND path=?', (harness, path)).fetchone()[0]

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
        c = self.connection
        where, params = [], []
        if start is not None:
            where.append('o.ts_us>=?'); params.append((start - _EPOCH) // _MICRO)
        if end is not None:
            where.append('o.ts_us<?'); params.append((end - _EPOCH) // _MICRO)
        for column, wanted in (('harness',effective_harness),('project_id',project),('session',raw_session),('turn_id',turn)):
            if wanted is not None:
                found = c.execute('SELECT id FROM strings WHERE value=?', (wanted,)).fetchone() if isinstance(wanted, str) else None
                if not found:
                    return []
                where.append(f'o.{column}=?'); params.append(found[0])
        where = ('WHERE ' + ' AND '.join(where)) if where else ''
        paths = {}
        for observation, path in c.execute('SELECT s.observation,f.path FROM sources s JOIN files f ON f.id=s.file'
                                           f' JOIN observations o ON o.id=s.observation {where}', params):
            paths.setdefault(observation, []).append(path)
        strings = {r[0]: r[1] for r in c.execute('SELECT id,value FROM strings')}
        result = []
        for row in c.execute(f'SELECT o.id,{",".join("o." + n for n in COLUMNS)} FROM observations o {where}', params):
            item = _decode(tuple(strings[v] if i in _REF_INDEX and v is not None else v
                                 for i, v in enumerate(row[1:])))
            item['sources'] = sorted(paths.get(row[0], ()))
            result.append(item)
        return sorted(result,key=lambda x:(x['ts'],x['provider'],x['id']))

    def doctor(self):
        count, first, last, incomplete, unlinked = self.connection.execute(
            'SELECT count(*), min(ts_us), max(ts_us), coalesce(sum(complete=0),0), coalesce(sum(turn_id IS NULL),0)'
            ' FROM observations').fetchone()
        imports = [json.loads(r[0]) for r in self.connection.execute('SELECT data FROM imports ORDER BY harness,root')]
        missing = sum(not Path(r[0]).exists() for r in self.connection.execute('SELECT path FROM files'))
        return dict(schema_version=SCHEMA_VERSION, machine=self.machine, observations=count,
                    first_event=None if first is None else _ts_text(first),
                    last_event=None if last is None else _ts_text(last),
                    missing_source_files=missing, imports=imports, coverage_complete=False,
                    incomplete_observations=incomplete, unlinked_turns=unlinked,
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
                    known_token_fields={k:known[k] if not rows or missing[k] < len(rows) else None for k in ALL_FIELDS},
                    missing_fields=missing, complete=complete)
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
    def instant(label):
        return label if granularity == 'day' else datetime.fromisoformat(label).astimezone(timezone.utc).isoformat()
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
                buckets=[dict(time=label,**aggregate(rows)) for label,rows in sorted(buckets.items(),key=lambda pair:instant(pair[0]))],
                groups=groups, coverage_complete=False, billing_verified=False)
