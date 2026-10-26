"""Developer acceptance uses the same TOML; legacy mcp.json remains optional."""

import json
import os
from pathlib import Path

from udb.config import load
from udb.launcher import command

ROOT = Path(__file__).resolve().parents[2]


def configuration():
    legacy = ROOT / "mcp.json"
    if legacy.exists():
        return json.loads(legacy.read_text())["mcpServers"]["universal-desktop-bridge"]
    settings = load(config=os.environ.get("UDB_TEST_CONFIG"))
    args = [
        "serve",
        "--home",
        str(settings.home),
        "--root",
        str(settings.project_root),
        "--enable-terminal" if settings.enable_terminal else "--disable-terminal",
    ]
    if settings.core_exe:
        args += ["--core-exe", str(settings.core_exe)]
    argv = command(args)
    return dict(command=argv[0], args=argv[1:])


def project_root():
    args = configuration()["args"]
    return (
        Path(args[args.index("--root") + 1])
        if "--root" in args
        else load(config=os.environ.get("UDB_TEST_CONFIG")).project_root
    )
