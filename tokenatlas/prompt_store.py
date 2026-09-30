"""Opt-in prompt text for the current top prompts: a 0600 side file next to the history, never in SQLite or snapshots."""
import hashlib
import json
import os
import stat
import sys
import time
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


def _warn(message):
    print(f'usage: warning: {message}',file=sys.stderr)


def _unsafe(st):
    """Why a store file must not be trusted (POSIX: symlink, non-regular, extra hard links, foreign owner, group/other access), else None.
    Windows has no such checks: the file there relies on the user profile's ACLs."""
    if os.name=='nt':return None
    if stat.S_ISLNK(st.st_mode):return 'a symlink'
    if not stat.S_ISREG(st.st_mode):return 'not a regular file'
    if st.st_nlink!=1:return 'hard-linked elsewhere'
    if st.st_uid!=os.getuid():return 'owned by another user'
    if st.st_mode&0o077:return f'accessible by others (mode {stat.S_IMODE(st.st_mode):o})'
    return None


def _read(path):
    """(raw bytes or None, refused reason or None). A missing file is (None, None); an unsafe or unreadable one is never read."""
    path=str(path)
    try:st=os.lstat(path)
    except FileNotFoundError:return None,None
    except OSError as exc:return None,f'cannot read: {exc}'
    bad=_unsafe(st)
    if bad:return None,bad
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            if os.name!='nt':
                now=os.fstat(fd)
                if (now.st_dev,now.st_ino)!=(st.st_dev,st.st_ino) or _unsafe(now):return None,'changed while opening'
            with os.fdopen(fd,'rb',closefd=False) as stream:return stream.read(),None
        finally:os.close(fd)
    except OSError as exc:return None,f'cannot read: {exc}'


def load_meta(path):
    """({(harness, session, turn_id): text or None}, k, by); a missing file is empty, a corrupt or unsafe one is empty plus a stderr warning."""
    raw,bad=_read(path)
    if bad:_warn(f'ignoring {path}: {bad}');return {},None,None
    if raw is None:return {},None,None
    try:
        data=json.loads(raw);entries=_entries(raw)
        return {(e['harness'],e['session'],e['turn_id']):e['text'] for e in entries},data.get('k'),data.get('by')
    except ValueError as exc:
        _warn(f'ignoring corrupt {path} ({exc})');return {},None,None


def load(path):
    return load_meta(path)[0]


def visible(path,records,table):
    """The stored texts whose prompt is in the current global top k (the store's recorded k and by): all a private report may embed."""
    texts,k,by=load_meta(path)
    if not texts or not isinstance(k,int) or by not in ('cost','tokens'):return {}
    top={(p['harness'],p['session'],p['turn_id']) for p in prompts.top_prompts(records,table,k,by)['prompts']}
    return {key:text for key,text in texts.items() if key in top}


def texts_hash(texts):
    """Stable digest of loaded texts for the report state; None when there are none."""
    if not texts:return None
    body=json.dumps(sorted([*k,v] for k,v in texts.items()),separators=(',',':'),ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def _clean_temps(directory):
    """Drop stale temp files of an interrupted write: ours, owned by this user, older than an hour."""
    cutoff=time.time()-3600
    for entry in Path(directory).glob('.top-prompts-*'):
        try:
            st=entry.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_mtime<cutoff and (os.name=='nt' or st.st_uid==os.getuid()):entry.unlink()
        except OSError:pass


def _write(path,data):
    path=Path(path);_clean_temps(path.parent)
    fd,tmp=tempfile.mkstemp(prefix='.top-prompts-',dir=path.parent)  # replace swaps the path itself, so a symlink there is replaced, never followed
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def update(path,records,table,machine,k=5,by='cost',extract=prompt_text.extract_prompt):
    """Keep text for the global top k prompts only: keep known entries, add local new ones, retry unreadable ones, evict the rest."""
    top=prompts.top_prompts(records,table,k,by)['prompts']
    old_raw,bad=_read(path)  # an unsafe file is not trusted: start over and replace it
    try:old={(e['harness'],e['session'],e['turn_id']):e for e in _entries(old_raw)} if old_raw else {}
    except ValueError:old={}  # corrupt: start over
    own={}  # prompt key -> sources of its own (non-subagent) observations
    for r,found in zip(records,prompts.assign_prompts(records)):
        if found and r['thread_kind']!='subagent':own.setdefault(found[:3],[]).extend(r.get('sources') or [])
    entries,added,kept=[],0,0
    for p in top:
        key=(p['harness'],p['session'],p['turn_id'])
        known=old.get(key)
        if known and (known['text'] is not None or p.get('machine')!=machine):entries.append(known);kept+=1;continue
        if p.get('machine')!=machine:continue  # only this machine's own logs can be read
        text=None
        for source in sorted(set(own.get(key,()))):
            text=extract(p['harness'],source,p['session'],p['turn_id'])
            if text:break
        if known and not text:entries.append(known);kept+=1;continue  # still unreadable: keep the entry as is
        entries.append({'harness':p['harness'],'session':p['session'],'turn_id':p['turn_id'],
                        'captured_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'text':text});added+=1
    evicted=len(set(old)-{(e['harness'],e['session'],e['turn_id']) for e in entries})
    data=json.dumps({'version':1,'k':k,'by':by,'entries':entries},sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()+b'\n'
    if data!=old_raw or bad:_write(path,data)
    return {'kept':kept,'added':added,'evicted':evicted,'path':str(path)}


def forget(path):
    """Delete the store; a symlink is unlinked, never followed. Warns when other hard links still hold the text."""
    try:
        st=os.lstat(path)
        if stat.S_ISREG(st.st_mode) and st.st_nlink>1:_warn(f'{path}: other hard links still hold the text')
        os.unlink(path)
    except FileNotFoundError:pass
