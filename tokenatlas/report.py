"""Self-contained offline report with an allowlisted, pseudonymized data boundary."""
from __future__ import annotations
import base64
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from tokenatlas import __version__
from tokenatlas import pricing, prompts
from tokenatlas.history import ALL_FIELDS

PUBLIC_NAMES = dict(
    provider=frozenset('anthropic openai openai-codex openrouter opencode berget google mistral'.split()),
    origin=frozenset(('cli', 'claude-desktop', 'sdk-cli', 'sdk-py', 'sdk-ts', 'codex-tui', 'codex_cli_rs',
                      'codex_exec', 'Codex Desktop', 'codex_work_desktop', 'vscode')),
    effort=frozenset('none minimal low medium high xhigh max ultra auto'.split()),
    harness=frozenset('claude codex pi opencode'.split()),
    thread_kind=frozenset('main subagent automation'.split()),
    turn_confidence=frozenset('observed derived absent'.split()),
)
CONSERVATIVE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._: -]{0,120}')
PUBLIC_MODEL = re.compile(
    r'(?:(?:openai|anthropic|google|qwen|z-ai|zai-org|mistralai|meta-llama|deepseek|moonshotai|x-ai)/)?'
    r'(?:claude|gpt|o[0-9]|codex|gemini|gemma|mistral|codestral|ministral|magistral|pixtral|devstral|qwen|llama|deepseek|glm|kimi|grok)'
    r'[A-Za-z0-9._-]{0,100}(?::free)?', re.IGNORECASE)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DICT_FIELDS = ('harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'turn_confidence', 'session',
               'parent_session', 'turn_id', 'agent', 'project_id', 'project_label', 'warnings', 'prompt')
WARNING_SEPARATOR = '\x1f'


def encode_columns(rows):
    """Columnar payload: dictionaries plus integer index columns; ts is delta-coded epoch ms and `off`
    (minutes east of UTC, dictionary-coded) lets the page derive local date/hour/minute exactly as Python did."""
    def dictionary(values):
        table, index = {}, []
        for value in values:
            index.append(table.setdefault(value, len(table)))
        return list(table), index
    dicts, idx = {}, {}
    for key in DICT_FIELDS:
        values = ([WARNING_SEPARATOR.join('' if w is None else w for w in r['warnings']) for r in rows]
                  if key == 'warnings' else [r[key] for r in rows])
        dicts[key], idx[key] = dictionary(values)
    dicts['off'], idx['off'] = dictionary(r['off'] for r in rows)
    ms = [r['ms'] for r in rows]
    ids = [r['id'] for r in rows]
    return dict(n=len(rows), dict=dicts, idx=idx, ts=[b - a for a, b in zip([0] + ms, ms)],
                id=ids, id_prefix='Observation ' if ids and all(isinstance(i, (int, type(None))) for i in ids) else None,
                tokens={k: [r['tokens'][k] for r in rows] for k in ALL_FIELDS},
                cost=[r['cost'] for r in rows], complete=[int(r['complete']) for r in rows], id_synthetic=[int(r['id_synthetic']) for r in rows])


COVERAGE_FIELDS = ('observations', 'first_event', 'last_event', 'missing_source_files',
                   'incomplete_observations', 'unlinked_turns')
IMPORT_FIELDS = ('harness', 'status', 'files_seen', 'files_parsed', 'malformed_lines', 'partial_lines',
                 'read_errors', 'unparsed_usage_lines', 'last_success')


def coverage_key(source_status):
    """Coverage subset embedded in a report, minus the volatile last_success timestamps."""
    key = {k: source_status.get(k) for k in COVERAGE_FIELDS}
    key['imports'] = [{k: imp.get(k) for k in IMPORT_FIELDS if k != 'last_success'}
                      for imp in source_status.get('imports', [])]
    return key


def report_state(revision, machine, spec, coverage, token=None, texts_hash=None):
    """(identity, data) 32-hex pair. Identity: version, options, database and template; data: revision token, counter, coverage and, for private reports, the stored prompt texts."""
    dump = lambda body: json.dumps(body, sort_keys=True, separators=(',', ':'))
    template = hashlib.sha256(Path(__file__).with_name('report_template.html').read_bytes()).hexdigest()
    identity = dump({'format': 2, 'version': __version__, 'spec': spec, 'machine': machine})
    data = dump({'token': token, 'revision': int(revision), 'coverage': coverage,
                 **({'prompt_texts': texts_hash} if texts_hash else {})})
    return tuple(hashlib.sha256(text.encode()).hexdigest()[:32] for text in (identity + template, data))


def build_report(records, source_status, timezone_name='Europe/Stockholm', redact=True, prompt_texts=None, table=None):
    """prompt_texts ({(harness, session, turn_id): text or None} from prompt_store) is for private reports only;
    table is the price table behind the per-observation list-price `cost` column (None = packaged prices)."""
    if redact and prompt_texts is not None:
        raise ValueError('prompt text cannot be included in a redacted report')
    table = table or pricing.load_prices()
    zone = ZoneInfo(timezone_name)
    records = sorted(records, key=lambda r: (r['ts'], r['harness'], r['id']))
    aliases = {}
    def alias(kind, value):
        if value in (None, '', 'unknown'):
            return None
        table = aliases.setdefault(kind, {})
        if value not in table:
            table[value] = f'{kind} {len(table) + 1:03d}'
        return table[value]
    projects = sorted({r.get('project_id') for r in records if r.get('project_id')})
    labels = {key: Path(key).name or 'Projekt' for key in projects}
    counts, seen = Counter(labels.values()), Counter()
    for key in projects:
        label = labels[key]
        if counts[label] > 1:
            seen[label] += 1
            labels[key] = f'{label} · {seen[label]}'
    def metadata(kind, value, record=None):
        if value is None or not redact:
            return value
        if kind == 'model':
            public = record.get('provider') in PUBLIC_NAMES['provider'] and PUBLIC_MODEL.fullmatch(str(value))
        elif record is None:
            public = CONSERVATIVE_NAME.fullmatch(str(value))
        else:
            public = isinstance(value, str) and value in PUBLIC_NAMES[kind]
        return value if public else alias(kind, str(value))
    assigned = prompts.assign_prompts(records)
    shown = {}  # prompt key -> display id
    rows = []
    for record in records:
        dt = datetime.fromisoformat(record['ts']).astimezone(zone)
        row = {key: metadata(key, record.get(key), record) for key in
               ('harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'turn_confidence')}
        oid = alias('Observation', record.get('id')) if redact else record.get('id')
        row['id'] = None if oid is None else int(oid.rsplit(' ', 1)[1]) if redact else oid
        for key, kind in (('session', 'Session'),
                           ('parent_session', 'Session'), ('turn_id', 'Tur'), ('agent', 'Agent')):
            value = record.get(key)
            if key in ('session', 'parent_session') and value not in (None, '', 'unknown'):
                value = f"{record['harness']}:{value}"
            row[key] = alias(kind, value) if redact else value
        cost = pricing.price_observation(record, table)['cost']
        found = assigned.get(record.get('id'))
        key = found and ':'.join(found[:3])
        if key and key not in shown:
            shown[key] = alias('Prompt', key) if redact else key
        row.update(prompt=key and shown[key], cost=cost and round(cost, 6), ts=record['ts'], ms=(dt - EPOCH) // timedelta(milliseconds=1),
                   off=int(dt.utcoffset().total_seconds() // 60),
                   project_id=alias('Projekt', record.get('project_id')),
                   project_label=(alias('Projekt', record.get('project_id')) if redact else
                                  labels.get(record.get('project_id'), 'Okänt projekt')),
                   tokens={key: record['tokens'].get(key) for key in ALL_FIELDS},
                   complete=bool(record['complete']), id_synthetic=bool(record['id_synthetic']),
                   warnings=[metadata('Varning', x) for x in record.get('warnings', [])])
        rows.append(row)
    coverage = {key: source_status.get(key) for key in COVERAGE_FIELDS}
    coverage.update(coverage_complete=False, billing_verified=False)
    coverage['imports'] = [{key: imp.get(key) for key in IMPORT_FIELDS} for imp in source_status.get('imports', [])]
    coverage['ranges'] = []
    for harness in sorted({r['harness'] for r in rows}):
        group = [r for r in rows if r['harness'] == harness]
        coverage['ranges'].append(dict(harness=harness, observations=len(group),
                                       first_event=group[0]['ts'], last_event=group[-1]['ts']))
    report = dict(version=2, generated_at=datetime.now(timezone.utc).isoformat(),
                  timezone=timezone_name, privacy='redacted' if redact else 'local',
                  columns=encode_columns(rows), coverage=coverage)
    if prompt_texts is not None:
        report['prompt_texts'] = {shown[':'.join(k)]: t for k, t in prompt_texts.items() if t and ':'.join(k) in shown}
    return report


STATE_META = re.compile(rb'<meta name="tokenatlas-state" content="([0-9a-f]{32})\.([0-9a-f]{32})">')


def read_report_state(path):
    """(identity, data) recorded in an existing regular report file (head only); None when absent, special or unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            found = STATE_META.search(stream.read(4096))
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
    return tuple(g.decode() for g in found.groups()) if found else None


def render_report(report, template=None, state=None):
    if template is None:
        template = Path(__file__).with_name('report_template.html').read_text(encoding='utf-8')
    if template.count('__USAGE_DATA__') != 1:
        raise ValueError('report template must contain exactly one data placeholder')
    payload = json.dumps(report, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    packed = base64.b64encode(gzip.compress(payload.encode('ascii'), compresslevel=9, mtime=0)).decode('ascii')
    html = template.replace('__USAGE_DATA__', packed)
    if state is not None:  # right after the charset meta, so it sits within the first bytes of the file
        marker = f'<meta name="tokenatlas-state" content="{state[0]}.{state[1]}">'
        html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + marker, 1)
    return html


def write_report(path, html):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.usage-report-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            os.chmod(temporary, 0o600)  # mkstemp already creates 0600; keep it explicit
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
