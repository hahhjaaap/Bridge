"""Real MCP denial/recovery and component tests using disposable owner state."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from udb.launcher import child_environment

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_protocol(tmp_path):
    home, project = tmp_path / "bridge", tmp_path / "project"
    project.mkdir()
    config = tmp_path / "udb-test.toml"
    config.write_text(
        "[bridge]\n"
        + "home = "
        + json.dumps(str(home))
        + "\nproject_root = "
        + json.dumps(str(project))
        + "\nenable_terminal = true\n",
        encoding="utf-8",
    )
    env = child_environment()
    env["UDB_TEST_CONFIG"] = str(config)
    env["UDB_HOME"] = str(home)
    return home, project, env


def test_isolated_component_optimization_scenarios(isolated_protocol):
    home, _, env = isolated_protocol
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/components/test_optimization.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert not (home / "state/computer-access.dpapi").exists()


def test_real_mcp_remains_usable_after_denied_operations_without_granting_access(
    isolated_protocol,
):
    home, project, env = isolated_protocol
    script = r"""
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_uia_provider import Client, object_data
client=Client()
try:
    tools=client.rpc("tools/list", {})["tools"]
    assert len(tools)==61
    assert not {"enable_computer_access", "grant_computer_access"}.intersection(t["name"] for t in tools)
    status=object_data(client.rpc("tools/call", {"name":"computer_status", "arguments":{}}))
    assert status["enabled"] is False
    for name, arguments in (
        ("spreadsheet", {"action":"write", "path":"must-not-exist.xlsx", "sheets":{"Data":[["fixture"]]}}),
        ("terminal_start", {"program":sys.executable, "arguments":["-c","print('must not execute')"]}),
    ):
        row=client.rpc("tools/call", {"name":name, "arguments":arguments}, allow_error=True)
        assert "error" in row or row.get("isError") is True, row
        if "error" in row:
            assert row["error"]["data"]["udb"]["replay_performed"] is False
    assert client.rpc("ping", {}) == {}
    assert not client.rpc("tools/call", {"name":"bridge_status", "arguments":{}}).get("isError")
finally:
    client.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "tests/components")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert not (project / "must-not-exist.xlsx").exists()
    assert not (home / "state/computer-access.dpapi").exists()
