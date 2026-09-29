"""python -m usage: local refresh, report, and doctor."""
import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import why
from usage import __version__
from usage.history import History, summarize
from usage.report import build_report, render_report, write_report


def main(argv=None):
    parser=argparse.ArgumentParser(description='Local observed token history; no network or LLM calls.')
    parser.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    default=Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))/'agentmon/history.sqlite3'
    parser.add_argument('--db',type=Path,default=default)
    commands=parser.add_subparsers(dest='command',required=True)
    refresh=commands.add_parser('refresh',help='Import changed files; preserve retained observations.')
    refresh.add_argument('--harness',choices=('claude','codex','pi','opencode'),required=True)
    refresh.add_argument('--root',type=Path,help='Override the harness session directory.')
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
        if args.command!='refresh' and not args.db.expanduser().is_file():
            raise ValueError('history database does not exist; run refresh first')
        with History(args.db) as history:
            if args.command=='refresh':
                roots={'claude':why.CLAUDE_PROJECTS,'codex':why.CODEX_SESSIONS,
                       'pi':why.PI_SESSIONS,'opencode':why.OPENCODE_DB}
                root=args.root or roots[args.harness]
                result=history.refresh(args.harness,root)
            elif args.command=='doctor':
                history.connection.execute('BEGIN')
                result=history.doctor()
            else:
                history.connection.execute('BEGIN')
                records=history.records(start,end,args.harness,args.project,args.session,args.turn)
                records=[row for row in records if all(getattr(args,key) is None or row[key]==getattr(args,key)
                    for key in ('model','effort','provider','agent'))]
                result=summarize(records,args.granularity,args.timezone)
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
