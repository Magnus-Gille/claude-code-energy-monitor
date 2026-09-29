"""python -m usage: local refresh, report, and doctor."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import why
from usage import __version__
from usage import sessions
from usage.history import History, summarize
from usage.report import build_report, render_report, write_report


def aggregate(results):
    """Fold per-root refresh results: summed counters, worst status, and the per-root list."""
    order=('ok','partial','missing')
    total={k:sum(r[k] for r in results) for k,v in results[0].items() if type(v) is int}
    return dict(harness=results[0]['harness'],status=max((r['status'] for r in results),key=lambda s:order.index(s) if s in order else len(order)),
                last_attempt=max(r['last_attempt'] for r in results),errors=[e for r in results for e in r['errors']],
                coverage_complete=False,roots=results,**total)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Local observed token history; no network or LLM calls.')
    parser.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    default=Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))/'agentmon/history.sqlite3'
    parser.add_argument('--db',type=Path,default=default)
    commands=parser.add_subparsers(dest='command',required=True)
    refresh=commands.add_parser('refresh',help='Import changed files; preserve retained observations.')
    refresh.add_argument('--harness',choices=('claude','codex','pi','opencode'),required=True)
    refresh.add_argument('--root',type=Path,help='Override the harness session directory.')
    snapshot=commands.add_parser('snapshot',help='Write a consistent private copy of the history database.')
    snapshot.add_argument('out',type=Path)
    importer=commands.add_parser('import',help="Merge another machine's snapshot into this database.")
    importer.add_argument('snapshot',type=Path)
    importer.add_argument('--label',required=True,help='Name for the source machine, e.g. pi:huginmunin.local.')
    report=commands.add_parser('report',help='Report saved observations without rereading source logs.')
    report.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    report.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    report.add_argument('--granularity',choices=('day','hour','minute'),default='day')
    report.add_argument('--timezone',default='Europe/Stockholm')
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
    overhead=commands.add_parser('overhead',help='Fixed context overhead: floor tokens, instruction and skill sizes.')
    overhead.add_argument('--refresh',action='store_true',help='Rescan the default session roots first.')
    overhead.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    overhead.add_argument('--since',help='Inclusive ISO timestamp of the session start.')
    overhead.add_argument('--json',action='store_true')
    commands.add_parser('doctor',help='Show source availability, import errors and known coverage limits.')
    args=parser.parse_args(argv)
    try:
        start=end=None
        if args.command=='report':
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
            if args.html and args.html.expanduser().resolve()==args.db.expanduser().resolve():
                raise ValueError('HTML output must not replace the history database')
        if args.command=='overhead':
            from usage import overhead as _overhead
            return _overhead.run(args)
        if args.command not in ('refresh','import') and not args.db.expanduser().is_file():
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
            if args.command=='refresh':
                roots={'claude':why.CLAUDE_PROJECTS,'codex':why.CODEX_SESSIONS,
                       'pi':why.PI_SESSIONS,'opencode':why.OPENCODE_DB}
                if args.root or args.harness!='claude':
                    result=history.refresh(args.harness,args.root or roots[args.harness])
                else:
                    # Main root, then each Cowork transcript root (macOS; absent elsewhere and simply skipped).
                    cowork=why.cowork_roots()
                    results=[history.refresh('claude',root) for root in [roots['claude'],*cowork]]
                    result=results[0] if len(results)==1 else aggregate(results)
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
                records=history.records(start,end,args.harness,args.project,args.session,args.turn)
                records=[row for row in records if all(getattr(args,key) is None or row[key]==getattr(args,key)
                    for key in ('model','effort','provider','agent'))]
                # The HTML path prints only a short receipt, so skip the (costly) JSON summary there.
                result={} if args.html else summarize(records,args.granularity,args.timezone)
                result['window']={'start':args.start,'end':args.end}
                result['source_status']=history.doctor()
                if args.records:result['records']=records
                if args.html:
                    payload=build_report(records,result['source_status'],args.timezone,redact=not args.private)
                    payload['initial_granularity']=args.granularity
                    write_report(args.html,render_report(payload))
                    result={'html':str(args.html.resolve()),'observations':len(records),
                            'privacy':payload['privacy'],'billing_verified':False,'coverage_complete':False}
        print(json.dumps(result,indent=2,sort_keys=True))
        return 0 if args.command!='refresh' or result['status']=='ok' else 2
    except (OSError,ValueError,sqlite3.Error,ZoneInfoNotFoundError) as exc:
        parser.exit(2,f'usage: {exc}\n')


if __name__=='__main__':raise SystemExit(main())
