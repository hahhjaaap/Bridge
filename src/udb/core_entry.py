"""Single entrypoint for source and frozen Windows EXE."""
import argparse
import os
import sys
from pathlib import Path

def main(argv=None):
    from .config import default_home
    base = default_home()
    parser = argparse.ArgumentParser(description='Universal Desktop Bridge')
    from udb import __version__
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--build-info', action='store_true', help='Print embedded build provenance without reading owner state')
    parser.add_argument('--list-tools', action='store_true', help='Print actual MCP tool schemas using disposable state')
    parser.add_argument('--home', default=str(base))
    parser.add_argument('--stdio', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--diagnostics', action='store_true')
    parser.add_argument('--probe', action='store_true', help='Fixed local MCP self-test using existing owner access; never grants permissions')
    parser.add_argument('--self-test-credentials', action='store_true',
                        help='Round-trip a synthetic credential in an isolated temporary target')
    parser.add_argument('--gui-check', action='store_true', help='Isolated native GUI self-test; never approves a real request')
    parser.add_argument('--gui-qa-dir', help='Optional screenshots of the isolated synthetic GUI self-test')
    parser.add_argument('--gui-screenshot', help='Save a native GUI screenshot and exit')
    parser.add_argument('--start-tunnel', action='store_true', help='Connect the existing configured tunnel after opening the GUI')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--headless', action='store_true', help='Run the windowless per-user manager (default)')
    mode.add_argument('--gui', action='store_true', help='Open the optional legacy settings window')
    parser.add_argument('--no-connect', action='store_true', help='Start the headless worker without a tunnel (offline diagnostics)')
    owner = parser.add_mutually_exclusive_group()
    owner.add_argument('--owner-computer-access', action='store_true', help='Attended local-console opt-in; never invoke through MCP')
    owner.add_argument('--revoke-computer-access', action='store_true', help='Revoke optional computer access locally')
    parser.add_argument('--legacy-gui', action='store_true')
    parser.add_argument('--host-workspace', help=argparse.SUPPRESS)
    parser.add_argument('--host-engine-qa', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--computer-primitives-qa', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--runtime-root',help=argparse.SUPPRESS)
    parser.add_argument('--runtime-terminal',choices=['on','off'],help=argparse.SUPPRESS)
    parser.add_argument('--runtime-core-exe',help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.runtime_root:
        from .config import load,activate
        activate(load(home=args.home,root=args.runtime_root,enable_terminal=args.runtime_terminal=='on',core_exe=args.runtime_core_exe))
    # Windowed PyInstaller builds may start with no stdout/stderr.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    if args.owner_computer_access or args.revoke_computer_access:
        from udb.core import Agent
        from udb.computer_access import enable_local, revoke_local
        try:
            result=(enable_local if args.owner_computer_access else revoke_local)(Agent(Path(args.home)))
            print('COMPUTER_ACCESS_ENABLED' if result['enabled'] else 'COMPUTER_ACCESS_DISABLED')
            return 0
        except Exception as exc:
            from udb.core import PolicyError
            print(str(exc) if isinstance(exc,PolicyError) else 'Computer access configuration failed; no OS permissions were elevated.',file=sys.stderr)
            return 1
    elif args.probe:
        import asyncio,json
        from udb.probe import run
        result=asyncio.run(run(Path(args.home)))
        print(json.dumps(result,ensure_ascii=True))
        return 0 if result['result']=='PASS' else 2
    elif args.computer_primitives_qa:
        import json
        from udb.computer_qa import run
        print(json.dumps(run(Path(args.home))))
    elif args.host_engine_qa:
        if os.name != 'nt': raise RuntimeError('Windows required')
        from udb.host_qa import run
        run(Path(args.home))
    elif args.host_workspace:
        if os.name != 'nt': raise RuntimeError('Windows required')
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
        from udb.host_workspace import run
        run(args.host_workspace)
    elif args.build_info:
        import json
        info = Path(__file__).resolve().parent/'build_info.json'
        result = json.loads(info.read_text('utf-8')) if info.is_file() else {'version':__version__, 'kind':'source', 'release_provenance':False}
        print(json.dumps(result))
    elif args.list_tools:
        import asyncio
        import json
        import tempfile
        from udb.core import Agent
        from udb.server import make_server
        with tempfile.TemporaryDirectory(prefix='udb-catalog-') as temp:
            server = make_server(Agent(Path(temp)))
            catalog = [tool.model_dump(mode='json', exclude_none=True)
                       for tool in asyncio.run(server.list_tools())]
        print(json.dumps({'version':__version__, 'tool_count':len(catalog), 'tools':catalog,
                          'chatgpt_discovery_verified':False}, ensure_ascii=True))
    elif args.gui_check:
        from udb.manager import self_test
        self_test(args.gui_qa_dir)
        print('NATIVE_GUI_SELFTEST=PASS')
    elif args.self_test_credentials:
        from udb.credentials import self_test
        self_test()
        print('SYNTHETIC_WINDOWS_CREDENTIAL_ROUNDTRIP=PASS')
    elif args.stdio or args.serve:
        from udb.server import serve
        serve(args.home, stdio=args.stdio)
    elif args.diagnostics:
        import json
        from udb.core import Agent
        print(json.dumps(Agent(Path(args.home)).diagnostics()))
    elif args.gui or args.legacy_gui or args.gui_screenshot:
        if args.legacy_gui:
            from udb.manager_legacy import run
            run(Path(args.home))
        else:
            from udb.manager import run
            run(Path(args.home), start_tunnel=args.start_tunnel, screenshot=args.gui_screenshot)
    else:
        from udb.headless import run
        return run(Path(args.home), connect=not args.no_connect)

if __name__ == '__main__':
    raise SystemExit(main())
