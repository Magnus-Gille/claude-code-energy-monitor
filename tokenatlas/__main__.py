"""python -m tokenatlas: local refresh, report, and doctor."""
import argparse
import json
import os
import sqlite3
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tokenatlas import why
from tokenatlas import __version__
from tokenatlas import sessions
from tokenatlas.history import History, summarize
from tokenatlas import pricing, prompt_store, prompts
from tokenatlas.report import build_report, coverage_key, read_report_state, render_report, report_state, write_report

DEFAULT_TIMEZONE='Europe/Stockholm'
FILTERS=('start','end','harness','project','session','turn','model','effort','provider','agent')


def aggregate(results):
    """Fold per-root refresh results: summed counters, worst status, and the per-root list."""
    order=('ok','partial','missing')
    total={k:sum(r[k] for r in results) for k,v in results[0].items() if type(v) is int}
    return dict(harness=results[0]['harness'],status=max((r['status'] for r in results),key=lambda s:order.index(s) if s in order else len(order)),
                last_attempt=max(r['last_attempt'] for r in results),errors=[e for r in results for e in r['errors']],
                coverage_complete=False,roots=results,**total)


def parse_duration(text):
    """'90s', '30m', '1h' or '2d' to seconds."""
    found=re.fullmatch(r'(\d+)([smhd])',text)
    if not found:raise ValueError(f'invalid duration {text!r}; use e.g. 90s, 30m, 1h or 2d')
    return int(found[1])*{'s':1,'m':60,'h':3600,'d':86400}[found[2]]


def _open_in_browser(path):
    where=f'could not open a browser; the report is at {path}'
    try:opened=webbrowser.open(Path(path).resolve().as_uri())
    except webbrowser.Error as exc:raise ValueError(f'{where} ({exc})') from exc
    if not opened:raise ValueError(where)


def _output_path(path,db):
    """Expanded HTML path; refuses the history database itself, also through a hard or symbolic link."""
    path,db=Path(path).expanduser(),Path(db).expanduser()
    if path.resolve()==db.resolve() or (path.exists() and db.exists() and os.path.samefile(path,db)):
        raise ValueError('HTML output must not replace the history database')
    return path


def _spec(privacy,timezone,granularity,filters):
    return {'privacy':privacy,'timezone':timezone,'granularity':granularity,'filters':{k:filters.get(k) for k in FILTERS}}


def _present(root):
    """False only when the root is definitely not there; an unreadable or wrong-type one is present and fails in refresh."""
    try:os.stat(root)
    except (FileNotFoundError,NotADirectoryError):return False
    except OSError:return True
    return True


def _with_problems(entry,problems):
    """Add Cowork traversal errors to a Claude refresh result and make it at least partial."""
    order=('ok','partial','missing','error')
    rank=lambda s:order.index(s) if s in order else len(order)
    return dict(entry,errors=[*entry.get('errors',[]),*problems],status=max(entry['status'],'partial',key=rank)) if problems else entry


def refresh_all(history):
    """Refresh every harness from its default roots; absent ones are reported, an OSError only fails its own harness."""
    roots={'claude':why.CLAUDE_PROJECTS,'codex':why.CODEX_SESSIONS,'pi':why.PI_SESSIONS,'opencode':why.OPENCODE_DB}
    order=('ok','partial','missing','error')
    rank=lambda s:order.index(s) if s in order else len(order)
    entries,worst=[],'ok'
    for name,root in roots.items():
        try:
            # Claude: main root, then each Cowork transcript root (macOS; absent elsewhere and simply skipped).
            cowork,problems=why.cowork_scan() if name=='claude' else ([],[])
            found=[r for r in [root,*cowork] if _present(r)]
            if not found and not problems:
                entries.append({'harness':name,'status':'absent'});continue
            results=[history.refresh(name,r) for r in found]
            entry=(results[0] if len(results)==1 else aggregate(results)) if results else {'harness':name,'status':'ok','errors':[]}
            entry=_with_problems(entry,problems)
        except OSError as exc:
            entry={'harness':name,'status':'error','errors':[f'{type(exc).__name__}: {exc}']}
        entries.append(entry)
        worst=max(worst,entry['status'],key=rank)
    return {'status':worst,'harnesses':entries}


def render_top(result,texts=None):
    """Compact table of ranked prompts; cost is list-price, '≥' when some requests could not be priced."""
    zone=ZoneInfo(DEFAULT_TIMEZONE)
    rows=[('#','when','harness','project','models','req','sub','Mtok','cost','resume')]
    for i,p in enumerate(result['prompts'],1):
        cost='n/a' if p['cost'] is None else ('' if p['cost_complete'] else '≥')+f"${p['cost']:.2f}"
        when=datetime.fromisoformat(p['first_ts']).astimezone(zone).strftime('%Y-%m-%d %H:%M')
        rows.append((str(i),when,p['harness'],p['project_label'] or '-',','.join(p['models']) or '-',str(p['requests']),
                     str(p['subagents']),f"{p['total_tokens']/1e6:.2f}",cost,p['resume'] or '-'))
    widths=[max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines=['  '.join(c.ljust(w) for c,w in zip(r,widths)).rstrip() for r in rows]
    if texts:  # stored previews go on an indented second line under their prompt
        shown=[]
        for line,p in zip(lines[1:],result['prompts']):
            shown.append(line)
            text=texts.get((p['harness'],p['session'],p['turn_id']))
            if text:shown.append('    '+text)
        lines=[lines[0],*shown]
    if len(result['prompts'])<result['total_prompts']:lines.append(f"showing {len(result['prompts'])} of {result['total_prompts']} prompts")
    return '\n'.join(lines)


def default_db():
    """Default history path; one-time move of the pre-rename agentmon directory (never used with --db)."""
    base=Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))
    new,old=base/'tokenatlas',base/'agentmon'
    if old.is_dir():
        if new.exists():
            print(f'warning: {old} left in place; using {new}',file=sys.stderr)
        else:
            os.rename(old,new)
            print(f'moved history from {old} to {new}',file=sys.stderr)
    return new/'history.sqlite3'


def _visible_texts(history,db):
    """The stored prompt texts a private report may embed (the current global top k); the history is read only when a store exists."""
    store=prompt_store.store_path(db)
    return prompt_store.visible(store,history.records(),pricing.load_prices()) if os.path.lexists(store) else {}


def main(argv=None):
    # Windows pipes default to a legacy code page without '≥' or '→'; replace such characters rather than crash.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(errors='replace')
    if Path(sys.argv[0]).name.lower() in ('energy-monitor','energy-monitor.exe','energy-monitor-script.py'):
        print('energy-monitor is deprecated; use tokenatlas',file=sys.stderr)
    parser=argparse.ArgumentParser(prog='tokenatlas',description='Local observed token history; no network or LLM calls.')
    parser.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    parser.add_argument('--db',type=Path,help='History database; default $XDG_STATE_HOME/tokenatlas/history.sqlite3.')
    commands=parser.add_subparsers(dest='command',required=True)
    refresh=commands.add_parser('refresh',help='Import changed files; preserve retained observations.')
    which=refresh.add_mutually_exclusive_group(required=True)
    which.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    which.add_argument('--all',action='store_true',help='Refresh every harness from its default roots; missing ones are reported as absent.')
    refresh.add_argument('--root',type=Path,help='Override the harness session directory (with --harness).')
    opener=commands.add_parser('open',help='Refresh, build the report (private by default) and open it in the browser.')
    opener.add_argument('--html',type=Path,help='Report path; default $XDG_STATE_HOME/tokenatlas/report.html.')
    opener.add_argument('--shared',action='store_true',help='Pseudonymize the report instead of keeping project labels.')
    opener.add_argument('--no-refresh',action='store_true',help='Use the saved history as it is.')
    snapshot=commands.add_parser('snapshot',help='Write a consistent private copy of the history database.')
    snapshot.add_argument('out',type=Path)
    importer=commands.add_parser('import',help="Merge another machine's snapshot into this database.")
    importer.add_argument('snapshot',type=Path)
    importer.add_argument('--label',required=True,help='Name for the source machine, e.g. pi:huginmunin.local.')
    report=commands.add_parser('report',help='Report saved observations without rereading source logs.')
    report.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    report.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    report.add_argument('--granularity',choices=('day','hour','minute'),default='day')
    report.add_argument('--timezone',default=DEFAULT_TIMEZONE)
    report.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    report.add_argument('--project',help='Exact full project identity, not basename.')
    report.add_argument('--session')
    report.add_argument('--turn')
    report.add_argument('--model',help='Exact model ID.')
    report.add_argument('--effort')
    report.add_argument('--provider')
    report.add_argument('--agent')
    report.add_argument('--html',type=Path,help='Write a standalone interactive offline HTML report.')
    report.add_argument('--private',action='store_true',help='Keep project labels and session IDs in HTML; default HTML uses pseudonyms.')
    report.add_argument('--if-changed',action='store_true',help='With --html: skip when the history revision matches the existing report.')
    report.add_argument('--max-age',help='With --html: skip when the existing report is younger than this (90s, 30m, 1h, 2d).')
    report.add_argument('--records',action='store_true',help='Include per-observation counters and source-file references. Reports contain private local paths.')
    for name,text in (('session','Show the session tree, per-model totals and outcomes for one root session.'),
                      ('rate','List threads of a session, or record an outcome rating for a unit.')):
        sub=commands.add_parser(name,help=text)
        sub.add_argument('id',help='Root session id, or harness:id when ambiguous.')
        sub.add_argument('--outcomes',type=Path,help='Outcomes JSONL; default outcomes.jsonl next to the database.')
        sub.add_argument('--prices',type=Path,help='Override the price table.')
        if name=='session':
            sub.add_argument('--json',action='store_true')
            sub.add_argument('--no-infer',action='store_true',help='Do not link headless children by time and cwd.')
        else:
            sub.add_argument('--unit');sub.add_argument('--thread',action='append',default=[])
            sub.add_argument('--outcome',choices=sessions.OUTCOMES);sub.add_argument('--note',default='')
    top=commands.add_parser('top',help='Rank the most expensive user prompts, subagent work rolled up into each.')
    top.add_argument('-n','--limit',type=int,default=5)
    top.add_argument('--by',choices=('cost','tokens'),default='cost')
    top.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    top.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    top.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    top.add_argument('--project',help='Exact full project identity, not basename.')
    top.add_argument('--prices',type=Path,help='Override the price table.')
    top.add_argument('--json',action='store_true')
    top.add_argument('--keep-text',action='store_true',help='Store the text of the current global top -n prompts in top-prompts.json next to the history (0600).')
    top.add_argument('--forget-text',action='store_true',help='Delete the stored prompt text.')
    top.add_argument('--with-text',action='store_true',help='With --json: include stored prompt text.')
    overhead=commands.add_parser('overhead',help='Fixed context overhead: floor tokens, instruction and skill sizes.')
    overhead.add_argument('--refresh',action='store_true',help='Rescan the default session roots first.')
    overhead.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    overhead.add_argument('--since',help='Inclusive ISO timestamp of the session start.')
    overhead.add_argument('--json',action='store_true')
    commands.add_parser('doctor',help='Show source availability, import errors and known coverage limits.')
    args=parser.parse_args(argv)
    if args.db is None:args.db=default_db()
    try:
        start=end=None
        if args.command=='refresh' and args.all and args.root:raise ValueError('--root cannot be used with --all')
        if args.command=='top' and args.limit<1:raise ValueError('--limit must be at least 1')
        if args.command=='top' and args.keep_text and args.forget_text:raise ValueError('--keep-text and --forget-text cannot be combined')
        if args.command in ('report','top'):
            if args.command=='report':
                max_age=parse_duration(args.max_age) if args.max_age is not None else None
                if (args.if_changed or max_age is not None) and not args.html:raise ValueError('--if-changed and --max-age need --html')
                ZoneInfo(args.timezone)
            for name in ('start','end'):
                value=getattr(args,name)
                if value:
                    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError(f'--{name} needs a timezone offset')
                    if name=='start':start=parsed
                    else:end=parsed
            if start and end and start>=end:raise ValueError('--start must precede --end')
            if args.command=='report' and args.html:_output_path(args.html,args.db)
        if args.command=='open':path=_output_path(args.html or args.db.parent/'report.html',args.db)
        if args.command=='overhead':
            from tokenatlas import overhead as _overhead
            return _overhead.run(args)
        if args.command not in ('refresh','import','open') and not args.db.expanduser().is_file():
            raise ValueError('history database does not exist; run refresh first')
        if args.command=='rate' and (args.unit or args.thread or args.outcome) and not (args.unit and args.thread and args.outcome):
            raise ValueError('rating needs --unit, --thread and --outcome')
        with History(args.db) as history:
            if args.command in ('session','rate'):
                history.connection.execute('BEGIN')
                price,retrieved=sessions.default_pricer(args.prices)
                result=sessions.build_tree(history.records(),args.id,infer=not getattr(args,'no_infer',False),price=price)
                path=args.outcomes or Path(args.db).with_name('outcomes.jsonl')
                rated=sessions.load_outcomes(path,args.id)
                if args.command=='session':
                    eff=sessions.efficiency(result,rated) if rated else None
                    if args.json:
                        result['efficiency']=eff;result['prices_retrieved']=retrieved
                        print(json.dumps(result,indent=2,sort_keys=True))
                    else:print(sessions.render(result,eff,retrieved))
                elif not args.unit:
                    threads=[n for n in sessions.iter_nodes(result['root']) if n is not result['root']]
                    if not threads:
                        print('no rateable threads: the root thread is coordination, not a unit of work',file=sys.stderr)
                    for node in threads:
                        print(f"{sessions.thread_key(node)}  {', '.join(node['models']) or '-'}  {sessions._tokens(node)}"
                              f"  {sessions._money(node['cost'],node['cost_coverage'],node['lower_bound'])}")
                else:
                    keys={sessions.thread_key(n) for n in sessions.iter_nodes(result['root']) if n is not result['root']}
                    for key in args.thread:
                        if key not in keys:raise ValueError(f'unknown thread {key!r}; run rate {args.id} to list threads')
                    line={'v':1,'root_session':result['root']['id'],'unit':args.unit,
                          'threads':[sessions._thread_of_key(k) for k in args.thread],'outcome':args.outcome,
                          'note':args.note,'ts':datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ')}
                    sessions.append_outcome(path,line)
                    print(json.dumps(line,sort_keys=True))
                return 0
            if args.command=='open':
                path=_output_path(path,args.db)  # the database may have just been created under a case alias
                if not args.no_refresh:
                    summary=refresh_all(history)
                    print('refresh: '+', '.join(f"{e['harness']} {e['status']}" for e in summary['harnesses']),file=sys.stderr)
                history.connection.execute('BEGIN')
                source_status=history.doctor()
                spec=_spec('redacted' if args.shared else 'local',DEFAULT_TIMEZONE,'day',{})
                texts=None if args.shared else _visible_texts(history,args.db)  # shared reports ignore the store
                state=report_state(history.revision,history.machine,spec,coverage_key(source_status),history.revision_token,prompt_store.texts_hash(texts))
                if path.exists() and read_report_state(path)==state:
                    result={'html':str(path.resolve()),'skipped':True,'reason':'unchanged'}
                else:
                    records=history.records()
                    payload=build_report(records,source_status,DEFAULT_TIMEZONE,redact=args.shared,prompt_texts=texts)
                    payload['initial_granularity']='day'
                    write_report(path,render_report(payload,state=state))
                    result={'html':str(path.resolve()),'observations':len(records),'privacy':payload['privacy']}
                _open_in_browser(path)
            elif args.command=='refresh' and args.all:
                result=refresh_all(history)
            elif args.command=='refresh':
                roots={'claude':why.CLAUDE_PROJECTS,'codex':why.CODEX_SESSIONS,
                       'pi':why.PI_SESSIONS,'opencode':why.OPENCODE_DB}
                if args.root or args.harness!='claude':
                    result=history.refresh(args.harness,args.root or roots[args.harness])
                else:
                    # Main root, then each Cowork transcript root (macOS; absent elsewhere and simply skipped).
                    cowork,problems=why.cowork_scan()
                    results=[history.refresh('claude',root) for root in [roots['claude'],*cowork]]
                    result=_with_problems(results[0] if len(results)==1 else aggregate(results),problems)
            elif args.command=='top':
                history.connection.execute('BEGIN')
                store=prompt_store.store_path(args.db)
                if args.forget_text:
                    prompt_store.forget(store)
                    print(json.dumps({'forgotten':str(store)}));return 0
                table=pricing.load_prices(args.prices)
                everything=history.records()  # rank over the whole history; the filters only choose which rows contribute
                kept=prompt_store.update(store,everything,table,history.machine,args.limit,args.by) if args.keep_text else None
                filtered=any(x is not None for x in (start,end,args.harness,args.project))
                keep={prompts.ident(r) for r in history.records(start,end,args.harness,args.project)} if filtered else None
                result=prompts.top_prompts(everything,table,args.limit,args.by,keep)
                texts=prompt_store.visible(store,everything,table)  # only the global top k: never text outside it
                if kept:result['text_store']=kept
                if not args.json:
                    print(render_top(result,texts))
                    if kept:print(f"kept text for {kept['kept']+kept['added']} prompts in {kept['path']} ({kept['added']} new, {kept['evicted']} evicted)",file=sys.stderr)
                    return 0
                if args.with_text:
                    for p in result['prompts']:p['text']=texts.get((p['harness'],p['session'],p['turn_id']))
            elif args.command=='snapshot':
                result=history.snapshot(args.out)
            elif args.command=='import':
                result=history.import_snapshot(args.snapshot,args.label)
                if result.get('warning'):print(f"usage: warning: {result['warning']}",file=sys.stderr)
            elif args.command=='doctor':
                history.connection.execute('BEGIN')
                result=history.doctor()
            else:
                history.connection.execute('BEGIN')
                source_status=history.doctor()
                if args.html:
                    path=_output_path(args.html,args.db)
                    spec=_spec('local' if args.private else 'redacted',args.timezone,args.granularity,vars(args))
                    texts=_visible_texts(history,args.db) if args.private else None
                    state=report_state(history.revision,history.machine,spec,coverage_key(source_status),history.revision_token,prompt_store.texts_hash(texts))
                    if path.exists() and (args.if_changed or max_age is not None):
                        found=read_report_state(path)
                        age=time.time()-path.stat().st_mtime
                        # Options (identity) are never throttled: only a data change on an otherwise identical report waits.
                        reason=('unchanged' if args.if_changed and found==state else
                                'too recent' if max_age is not None and found is not None and found[0]==state[0] and 0<=age<max_age else None)
                        if reason:
                            print(json.dumps({'html':str(path.resolve()),'skipped':True,'reason':reason}))
                            return 0
                records=history.records(start,end,args.harness,args.project,args.session,args.turn)
                records=[row for row in records if all(getattr(args,key) is None or row[key]==getattr(args,key)
                    for key in ('model','effort','provider','agent'))]
                # The HTML path prints only a short receipt, so skip the (costly) JSON summary there.
                result={} if args.html else summarize(records,args.granularity,args.timezone)
                result['window']={'start':args.start,'end':args.end}
                result['source_status']=source_status
                if args.records:result['records']=records
                if args.html:
                    if texts is not None:
                        filtered=any(getattr(args,key) is not None for key in ('start','end','harness','project','session','turn','model','effort','provider','agent'))
                        texts=prompt_store.visible(prompt_store.store_path(args.db),history.records() if filtered else records,pricing.load_prices())
                    payload=build_report(records,source_status,args.timezone,redact=not args.private,prompt_texts=texts)
                    payload['initial_granularity']=args.granularity
                    write_report(path,render_report(payload,state=state))
                    result={'html':str(path.resolve()),'observations':len(records),
                            'privacy':payload['privacy'],'billing_verified':False,'coverage_complete':False}
        print(json.dumps(result,indent=2,sort_keys=True))
        return 0 if args.command!='refresh' or result['status']=='ok' else 2
    except (OSError,ValueError,sqlite3.Error,ZoneInfoNotFoundError) as exc:
        parser.exit(2,f'usage: {exc}\n')


if __name__=='__main__':raise SystemExit(main())
