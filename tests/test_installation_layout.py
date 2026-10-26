"""Regression checks for the installed UDB single-source layout."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from udb import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = PROJECT_ROOT.parents[1]


def test_installed_tree_uses_one_canonical_runtime():
    required = (
        INSTALL_ROOT / "plugin.json",
        INSTALL_ROOT / "mcp.json",
        INSTALL_ROOT / "udb.toml",
    )
    if not all(path.is_file() for path in required):
        pytest.skip("The local installed UDB layout is not present.")

    source_manifest = json.loads(
        (PROJECT_ROOT / "plugin.json").read_text(encoding="utf-8")
    )
    installed_manifest = json.loads(
        (INSTALL_ROOT / "plugin.json").read_text(encoding="utf-8")
    )
    assert installed_manifest == source_manifest
    assert installed_manifest["name"] == "universal-desktop-bridge"
    assert installed_manifest["version"] == __version__ == "0.9.1"

    config = json.loads((INSTALL_ROOT / "mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["universal-desktop-bridge"]
    canonical_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    assert Path(server["command"]).resolve() == canonical_python.resolve()
    assert server["args"] == [
        "-m",
        "udb",
        "serve",
        "--config",
        str(INSTALL_ROOT / "udb.toml"),
    ]
    assert server["cwd"] == "$" + "{PLUGIN_ROOT}"

    runtime = tomllib.loads((INSTALL_ROOT / "udb.toml").read_text(encoding="utf-8"))[
        "bridge"
    ]
    assert Path(runtime["home"]).resolve() == INSTALL_ROOT.resolve()
    assert Path(runtime["project_root"]).is_dir()
    assert runtime["core_backend"] == "source"
    assert isinstance(runtime["enable_terminal"], bool)
    assert not {
        "api_key",
        "token",
        "secret",
        "password",
        "credentials",
    }.intersection(runtime)

    active_projects = {
        item.name for item in (INSTALL_ROOT / "projects").iterdir() if item.is_dir()
    }
    assert {"udb", "demo", "releases"} <= active_projects
    assert not active_projects.intersection(
        {"refactor-0.9.1", "udb-runtime-0.9.0", "unified", "legacy"}
    )

    shim = (INSTALL_ROOT / "UDB-Command" / "udb_cli.py").read_text(encoding="utf-8")
    assert 'root / "projects" / "udb"' in shim
    assert "udb-runtime-0.9.0" not in shim
    assert "subprocess.run" in shim
