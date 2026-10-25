"""The only public entry point: udb (also python -m udb)."""
import argparse
import json
from pathlib import Path
import sys
from ._version import VERSION
from .config import load

def worker(role,args):
    if role=='core':
        from .core_entry import main
        return main(args)
    if role=='companion':
        from .companion.runtime import main
        return main(args)
    if role=='document':
        from .companion.documents import worker as run
        return run()
    if role=='search':
        from .companion.background import search_worker
        return search_worker()
    if role=='legacy-proxy':
        p=argparse.ArgumentParser();p.add_argument('--udb-home',required=True);p.add_argument('--companion',required=True);p.add_argument('--root',required=True);p.add_argument('--enable-terminal',action='store_true');p.add_argument('--core-exe');a=p.parse_args(args)
        from .proxy import run
        settings=load(home=a.udb_home,root=a.root,enable_terminal=a.enable_terminal,core_exe=a.core_exe)
        return run(settings)
    raise ValueError('Unknown worker role')

def add_settings(parser,suppress=False):
    default=argparse.SUPPRESS if suppress else None
    for name in ('home','config','root','core-exe'):parser.add_argument('--'+name,default=default)
    terminal=parser.add_mutually_exclusive_group()
    terminal.add_argument('--enable-terminal',dest='enable_terminal',action='store_true',default=default)
    terminal.add_argument('--disable-terminal',dest='enable_terminal',action='store_false',default=default)

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv[:1]==['_worker']:
        if len(argv)<2:raise SystemExit('Worker role required')
        return worker(argv[1],argv[2:])
    parser=argparse.ArgumentParser(prog='udb',description='Universal Desktop Bridge: one package, one command')
    parser.add_argument('--version',action='version',version=VERSION);add_settings(parser)
    sub=parser.add_subparsers(dest='action')
    for name in ('start','serve','status','stop','doctor','test','tools','init','build','build-info','manifest'):
        item=sub.add_parser(name);add_settings(item,True)
        if name in ('start','status','stop','doctor','test'):
            item.add_argument('--json',action='store_true');item.add_argument('--wait',type=int,default=30)
        if name=='build':
            item.add_argument('--exe',action='store_true');item.add_argument('--output')
        if name=='serve':item.add_argument('--stdio',action='store_true',help='stdio is the default transport')
    parser.add_argument('--access',choices=['computer'],help='Attended local owner opt-in only')
    args=parser.parse_args(argv);action=args.action or 'start'
    try:
        if action=='manifest':
            from .metadata import manifest
            print(json.dumps(manifest(),indent=2));return 0
        if action=='build':
            from .build import run
            return run(args.exe,args.output)
        if action=='build-info':
            from .build import build_info
            print(json.dumps(build_info(),sort_keys=True));return 0
        if action=='tools':
            from .registry import catalog
            print(json.dumps(dict(version=VERSION,**catalog()),ensure_ascii=True));return 0
        settings=load(home=args.home,config=args.config,root=args.root,enable_terminal=args.enable_terminal,core_exe=args.core_exe)
        if action=='init':
            settings.home.mkdir(parents=True,exist_ok=True);target=settings.home/'udb.toml'
            with target.open('x',encoding='utf-8') as f:f.write('[bridge]\ncore_backend = "source"\nenable_terminal = false\n')
            print('Created runtime configuration; no connection or permission was granted.');return 0
        if action=='serve':
            if args.access:raise ValueError('Owner activation is not an MCP operation')
            from .proxy import run
            return run(settings)
        from . import owner_command
        legacy=[action,'--home',str(settings.home),'--wait',str(getattr(args,'wait',30))]
        if getattr(args,'json',False):legacy.append('--json')
        if args.access:legacy+=['--access',args.access]
        from .config import activate
        previous=activate(settings)
        try:return owner_command.main(legacy)
        finally:activate(previous)
    except (ValueError,OSError) as exc:
        print('UDB: '+str(exc),file=sys.stderr);return 2
