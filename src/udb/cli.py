"""The public command-line entry point: udb or python -m udb."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from ._version import VERSION
from .config import Settings, activate, checked, load, serve_options


def _run_legacy_proxy(args: Sequence[str]) -> int:
    """Keep the old proxy worker arguments callable for existing launchers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--udb-home", required=True)
    parser.add_argument("--companion", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--enable-terminal", action="store_true")
    parser.add_argument("--core-exe")
    parsed = parser.parse_args(args)

    from .proxy import run

    settings = load(
        home=parsed.udb_home,
        root=parsed.root,
        enable_terminal=parsed.enable_terminal,
        core_exe=parsed.core_exe,
    )
    return run(settings)


def worker(role: str, args: Sequence[str]) -> int:
    """Dispatch private worker processes started by the package launcher."""
    if role == "core":
        from .core_entry import main

        return main(args)
    if role == "companion":
        from .companion.runtime import main

        return main(args)
    if role == "document":
        from .companion.documents import worker as run

        return run()
    if role == "search":
        from .companion.background import search_worker

        return search_worker()
    if role == "legacy-proxy":
        return _run_legacy_proxy(args)
    raise ValueError("Unknown worker role")


def add_settings(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Register common path and terminal settings on a CLI parser."""
    default = argparse.SUPPRESS if suppress else None
    for name in ("home", "config", "root", "core-exe"):
        parser.add_argument(f"--{name}", default=default)

    terminal = parser.add_mutually_exclusive_group()
    terminal.add_argument(
        "--enable-terminal",
        dest="enable_terminal",
        action="store_true",
        default=default,
    )
    terminal.add_argument(
        "--disable-terminal",
        dest="enable_terminal",
        action="store_false",
        default=default,
    )


def _add_subcommands(parser: argparse.ArgumentParser) -> None:
    subcommands = parser.add_subparsers(dest="action")
    command_names = (
        "start",
        "setup",
        "gui",
        "runtime",
        "serve",
        "status",
        "logs",
        "stop",
        "doctor",
        "test",
        "revoke-access",
        "allow-source",
        "revoke-source",
        "tools",
        "init",
        "build",
        "build-info",
        "manifest",
        "mcp-config",
    )
    for name in command_names:
        command = subcommands.add_parser(name)
        add_settings(command, suppress=True)
        if name in ("start", "status", "stop", "doctor", "test", "revoke-access"):
            command.add_argument(
                "--json", action="store_true", default=argparse.SUPPRESS
            )
            command.add_argument("--wait", type=int, default=argparse.SUPPRESS)
            command.add_argument(
                "--access", choices=("computer",), default=argparse.SUPPRESS
            )
        if name == "start":
            view = command.add_mutually_exclusive_group()
            view.add_argument(
                "--detach", action="store_true", default=argparse.SUPPRESS
            )
            view.add_argument(
                "--follow", action="store_true", default=argparse.SUPPRESS
            )
            command.add_argument(
                "--offline", action="store_true", default=argparse.SUPPRESS
            )
        if name == "setup":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument(
                "--edit",
                action="store_true",
                help="Re-enter connection details in the attended terminal",
            )
            mode.add_argument(
                "--local",
                action="store_true",
                help="Remember local mode without a ChatGPT connection",
            )
        if name == "runtime":
            command.add_argument("--probe", action="store_true")
        if name == "logs":
            command.add_argument(
                "--once", action="store_true", help="Print retained activity and exit"
            )
            command.add_argument("--tail", type=int, default=30, metavar="0..2000")
            command.add_argument(
                "--details",
                action="store_true",
                help="Show safe argument/result metadata",
            )
            command.add_argument(
                "--json", action="store_true", default=argparse.SUPPRESS
            )
        if name in ("allow-source", "revoke-source"):
            command.add_argument(
                "--path", required=True, help="Exact protected source project folder"
            )
        if name == "build":
            command.add_argument("--exe", action="store_true")
            command.add_argument("--output")
            command.add_argument(
                "--assets", help="Inventoried public runtime helper directory"
            )
        if name == "serve":
            command.add_argument(
                "--stdio", action="store_true", help="stdio is the default transport"
            )


def _initialize_config(args: argparse.Namespace, settings: Settings) -> int:
    target = (
        checked(args.config) if args.config else checked(settings.home / "udb.toml")
    )
    values = {
        "home": str(settings.home),
        "project_root": str(settings.project_root),
        "core_backend": settings.core_backend,
        "enable_terminal": settings.enable_terminal,
    }
    if settings.core_exe:
        values["core_exe"] = str(settings.core_exe)

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(
            "[bridge]\n"
            + "".join(
                f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
                for key, value in values.items()
            )
        )
    print("Created runtime configuration; no connection or permission was granted.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_worker"]:
        if len(argv) < 2:
            raise SystemExit("Worker role required")
        return worker(argv[1], argv[2:])

    parser = argparse.ArgumentParser(
        prog="udb",
        description="Universal Desktop Bridge: one package, one command",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    add_settings(parser)
    parser.add_argument("--json", action="store_true")
    view = parser.add_mutually_exclusive_group()
    view.add_argument(
        "--detach", action="store_true", help="Do not attach live logs after startup"
    )
    view.add_argument(
        "--follow", action="store_true", help="Force live logs after startup"
    )
    parser.add_argument("--wait", type=int, default=30)
    parser.add_argument(
        "--offline", action="store_true", help="Start local agent without a tunnel"
    )
    _add_subcommands(parser)
    parser.add_argument(
        "--access", choices=("computer",), help="Attended local owner opt-in only"
    )

    args = parser.parse_args(argv)
    action = args.action or "start"
    try:
        if (args.detach or args.follow) and action != "start":
            raise ValueError("--detach/--follow apply only to start")
        if args.follow and args.json:
            raise ValueError("--follow cannot be combined with --json")
        if action == "manifest":
            from .metadata import manifest

            print(json.dumps(manifest(), indent=2))
            return 0
        if action == "build":
            from .build import run

            return run(args.exe, args.output, assets=args.assets)
        if action == "runtime":
            from .components import runtime_report

            print(json.dumps(runtime_report(args.probe), ensure_ascii=True))
            return 0
        if action == "build-info":
            from .build import build_info

            print(json.dumps(build_info(), sort_keys=True))
            return 0
        if action == "tools":
            from .registry import catalog

            print(json.dumps({"version": VERSION, **catalog()}, ensure_ascii=True))
            return 0

        settings = load(
            home=args.home,
            config=args.config,
            root=args.root,
            enable_terminal=args.enable_terminal,
            core_exe=args.core_exe,
            allow_missing_config=action == "init",
        )
        if action == "setup":
            if args.json or args.access or args.offline:
                raise ValueError(
                    "Use udb setup directly in your own terminal without --json/--access/--offline"
                )
            from .onboarding import run

            previous = activate(settings)
            try:
                if args.edit or args.local:
                    return run(settings, edit=args.edit, local=args.local)
                return run(settings)
            except KeyboardInterrupt:
                print("\nSetup canceled.", file=sys.stderr)
                return 130
            finally:
                activate(previous)
        if action == "start":
            from . import onboarding

            startup_state = {}
            if not args.offline:
                preferred = onboarding.saved_mode(settings)
                from . import owner_command

                startup_state = owner_command.headless_state(settings.home) or {}
                if startup_state.get("connection_mode") == "offline" or (
                    not startup_state and preferred == "offline"
                ):
                    args.offline = True
            if (
                not (args.offline or args.detach or args.json or args.access)
                and not startup_state
                and onboarding.attended_console()
                and not onboarding.saved_connection(settings)
            ):
                previous = activate(settings)
                try:
                    return onboarding.run(settings)
                except KeyboardInterrupt:
                    print("\nSetup canceled.", file=sys.stderr)
                    return 130
                finally:
                    activate(previous)
        if action == "mcp-config":
            from .launcher import command

            previous = activate(settings)
            try:
                argv = command(serve_options(settings.home))
            finally:
                activate(previous)
            print(
                json.dumps(
                    {
                        "mcpServers": {
                            "universal-desktop-bridge": {
                                "type": "stdio",
                                "command": argv[0],
                                "args": argv[1:],
                            }
                        }
                    },
                    indent=2,
                )
            )
            return 0
        if args.offline and action != "start":
            raise ValueError("--offline applies only to start")
        if action == "init":
            return _initialize_config(args, settings)
        if action == "logs":
            if args.access:
                raise ValueError("Log viewing does not activate owner access")
            from .activity import watch

            return watch(
                settings.home,
                tail=args.tail,
                once=args.once,
                details=args.details,
                as_json=args.json,
            )
        if action == "gui":
            if args.access:
                raise ValueError(
                    "Owner activation requires the separate attended command"
                )
            import subprocess

            from .config import prepare_workspace
            from .launcher import worker_command

            prepare_workspace(settings)
            previous = activate(settings)
            try:
                from .config import manager_options

                return subprocess.run(
                    worker_command(
                        "core",
                        [
                            "--gui",
                            "--home",
                            str(settings.home),
                            *manager_options(settings.home),
                        ],
                    ),
                    shell=False,
                ).returncode
            finally:
                activate(previous)
        if action in ("allow-source", "revoke-source"):
            if args.access or args.json:
                raise ValueError(
                    "Source permission requires a separate attended local command"
                )
            from .source_access import command

            return command(settings.home, args.path, revoke=action == "revoke-source")
        if action == "serve":
            if args.access:
                raise ValueError("Owner activation is not an MCP operation")
            from .config import prepare_workspace
            from .proxy import run

            prepare_workspace(settings)
            return run(settings)

        from . import owner_command

        legacy_args = [
            action,
            "--home",
            str(settings.home),
            "--wait",
            str(getattr(args, "wait", 30)),
        ]
        if getattr(args, "json", False):
            legacy_args.append("--json")
        if args.detach:
            legacy_args.append("--detach")
        if args.follow:
            legacy_args.append("--follow")
        if args.access:
            legacy_args.extend(["--access", args.access])
        if args.offline:
            legacy_args.append("--offline")

        previous_settings = activate(settings)
        try:
            return owner_command.main(legacy_args)
        finally:
            activate(previous_settings)
    except (ValueError, OSError) as exc:
        print(f"UDB: {exc}", file=sys.stderr)
        return 2
