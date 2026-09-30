"""Opt-in prompt text for the current top prompts: a 0600 side file next to the history, never in SQLite or snapshots."""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import prompt_text, prompts

FILE = 'top-prompts.json'


def store_path(db_path):
    return Path(db_path).expanduser().with_name(FILE)


def _entries(raw):
    """Validated entry dicts from file bytes; ValueError when the file is not a well-formed store."""
    try:
        entries=json.loads(raw)['entries']
        for e in entries:
            if not (isinstance(e['text'],(str,type(None))) and all(isinstance(e[k],str) for k in ('harness','session','turn_id'))):raise ValueError('bad entry')
        return entries
    except (ValueError,KeyError,TypeError) as exc:raise ValueError(f'{type(exc).__name__}: {exc}') from exc


def load(path):
    """{(harness, session, turn_id): text or None}; a missing file is {}, a corrupt one is {} plus a stderr warning."""
    try:raw=Path(path).read_bytes()
    except FileNotFoundError:return {}
    except OSError as exc:
        print(f'usage: warning: cannot read {path}: {exc}',file=sys.stderr);return {}
    try:return {(e['harness'],e['session'],e['turn_id']):e['text'] for e in _entries(raw)}
    except ValueError as exc:
        print(f'usage: warning: ignoring corrupt {path} ({exc})',file=sys.stderr);return {}


def texts_hash(texts):
    """Stable digest of loaded texts for the report state; None when there are none."""
    if not texts:return None
    body=json.dumps(sorted([*k,v] for k,v in texts.items()),separators=(',',':'),ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def _write(path,data):
    path=Path(path);fd,tmp=tempfile.mkstemp(prefix='.top-prompts-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def update(path,records,table,machine,k=5,by='cost',extract=prompt_text.extract_prompt):
    """Keep text for the global top k prompts only: keep known entries, add local new ones, evict the rest."""
    top=prompts.top_prompts(records,table,k,by)['prompts']
    try:old_raw=Path(path).read_bytes()
    except OSError:old_raw=None
    try:old={(e['harness'],e['session'],e['turn_id']):e for e in _entries(old_raw)} if old_raw else {}
    except ValueError:old={}  # corrupt: start over
    assigned=prompts.assign_prompts(records)
    own={}  # prompt key -> sources of its own (non-subagent) observations
    for r in records:
        found=assigned.get(r['id'])
        if found and r['thread_kind']!='subagent':own.setdefault(found[:3],[]).extend(r.get('sources') or [])
    entries,added,kept=[],0,0
    for p in top:
        key=(p['harness'],p['session'],p['turn_id'])
        if key in old:entries.append(old[key]);kept+=1;continue
        if p.get('machine')!=machine:continue  # only this machine's own logs can be read
        text=None
        for source in sorted(set(own.get(key,()))):
            text=extract(p['harness'],source,p['session'],p['turn_id'])
            if text:break
        entries.append({'harness':p['harness'],'session':p['session'],'turn_id':p['turn_id'],
                        'captured_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'text':text});added+=1
    evicted=len(set(old)-{(e['harness'],e['session'],e['turn_id']) for e in entries})
    data=json.dumps({'version':1,'k':k,'by':by,'entries':entries},sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()+b'\n'
    if data!=old_raw:_write(path,data)
    return {'kept':kept,'added':added,'evicted':evicted,'path':str(path)}


def forget(path):
    try:os.unlink(path)
    except FileNotFoundError:pass
