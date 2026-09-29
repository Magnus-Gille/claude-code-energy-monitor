"""Self-contained offline report with an allowlisted, pseudonymized data boundary."""
from __future__ import annotations
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from usage.history import ALL_FIELDS


def build_report(records, source_status, timezone_name='Europe/Stockholm', redact=True):
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
    def metadata(kind, value):
        if value is None:
            return None
        if redact and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._: -]{0,120}', str(value)):
            return alias(kind, str(value))
        return value
    rows = []
    for record in records:
        dt = datetime.fromisoformat(record['ts']).astimezone(zone)
        row = {key: metadata(key, record.get(key)) for key in
               ('harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'turn_confidence')}
        for key, kind in (('id', 'Observation'), ('session', 'Session'),
                           ('parent_session', 'Session'), ('turn_id', 'Tur'), ('agent', 'Agent')):
            value = record.get(key)
            if key in ('session', 'parent_session') and value not in (None, '', 'unknown'):
                value = f"{record['harness']}:{value}"
            row[key] = alias(kind, value) if redact else value
        row.update(ts=record['ts'], date=dt.date().isoformat(),
                   hour=dt.replace(minute=0, second=0, microsecond=0).isoformat(),
                   minute=dt.replace(second=0, microsecond=0).isoformat(),
                   project_id=alias('Projekt', record.get('project_id')),
                   project_label=(alias('Projekt', record.get('project_id')) if redact else
                                  labels.get(record.get('project_id'), 'Okänt projekt')),
                   tokens={key: record['tokens'].get(key) for key in ALL_FIELDS},
                   complete=bool(record['complete']), id_synthetic=bool(record['id_synthetic']),
                   warnings=[metadata('Varning', x) for x in record.get('warnings', [])])
        rows.append(row)
    coverage = {key: source_status.get(key) for key in
                ('observations', 'first_event', 'last_event', 'missing_source_files',
                 'incomplete_observations', 'unlinked_turns')}
    coverage.update(coverage_complete=False, billing_verified=False)
    coverage['imports'] = [{key: imp.get(key) for key in
                           ('harness', 'status', 'files_seen', 'files_parsed', 'malformed_lines',
                            'partial_lines', 'read_errors', 'unparsed_usage_lines', 'last_success')}
                          for imp in source_status.get('imports', [])]
    coverage['ranges'] = []
    for harness in sorted({r['harness'] for r in rows}):
        group = [r for r in rows if r['harness'] == harness]
        coverage['ranges'].append(dict(harness=harness, observations=len(group),
                                       first_event=group[0]['ts'], last_event=group[-1]['ts']))
    return dict(version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                timezone=timezone_name, privacy='redacted' if redact else 'local',
                records=rows, coverage=coverage)


def render_report(report, template=None):
    if template is None:
        template = Path(__file__).with_name('report_template.html').read_text(encoding='utf-8')
    if template.count('__USAGE_DATA__') != 1:
        raise ValueError('report template must contain exactly one data placeholder')
    payload = json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for char, escaped in (('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'),
                          ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        payload = payload.replace(char, escaped)
    return template.replace('__USAGE_DATA__', payload)


def write_report(path, html):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.usage-report-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
