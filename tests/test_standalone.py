"""Standalone packaging, offline startup and embedded-worker regressions.

All application homes, helper fixtures and permission checks are isolated.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import build, cli, components, config, desktop, headless, owner_command
from udb.core import PolicyError


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    (root / "bin").mkdir(parents=True)
    (root / "licenses").mkdir()
    binary = root / "bin" / "tunnel-client.exe"
    binary.write_bytes(b"synthetic helper; not executable")
    (root / "licenses" / "tunnel-client.txt").write_text("synthetic notice")
    record = {
        "name": "tunnel-client",
        "version": "fixture",
        "binary": "bin/tunnel-client.exe",
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "license": "licenses/tunnel-client.txt",
        "source_url": "https://example.test/public-helper.zip",
    }
    manifest = root / "components.json"
    manifest.write_text(json.dumps({"schema": 1, "components": [record]}))
    return root, record


def test_asset_inventory_matches_binary_and_contains_no_owner_paths(assets):
    root, record = assets
    assert components.verified_assets(root) == [record]


@pytest.mark.parametrize(
    "field,value",
    [
        ("binary", "../private.exe"),
        ("binary", "bin/another.exe"),
        ("license", "../owner.key"),
        ("name", "arbitrary-program"),
        ("sha256", "0" * 64),
        ("sha256", None),
        ("version", ""),
        ("source_url", "http://example.test/helper"),
        ("source_url", "https://user:password@example.test/helper"),
        ("source_url", "https://example.test/helper?token=private"),
    ],
)
def test_invalid_asset_provenance_fails_closed(assets, field, value):
    root, record = assets
    record[field] = value
    (root / "components.json").write_text(
        json.dumps({"schema": 1, "components": [record]})
    )
    with pytest.raises(ValueError):
        components.verified_assets(root)


def test_duplicate_component_or_extra_inventory_field_is_rejected(assets):
    root, record = assets
    for data in (
        {"schema": 1, "components": [record, record]},
        {"schema": 1, "components": [record], "owner_key": "not-a-key"},
    ):
        (root / "components.json").write_text(json.dumps(data))
        with pytest.raises(ValueError):
            components.verified_assets(root)


def test_missing_component_manifest_means_no_bundled_helpers(tmp_path):
    assert components.verified_assets(tmp_path / "missing") == []


def test_corrupt_bundle_never_falls_back_to_an_external_helper(
    assets, tmp_path, monkeypatch
):
    root, _ = assets
    monkeypatch.setattr(components, "bundle_root", lambda: root)
    home = tmp_path / "home"
    (home / "tools").mkdir(parents=True)
    (home / "tools" / "tunnel-client.exe").write_bytes(b"external fixture")
    assert (
        components.component_path("tunnel-client", home)
        == root / "bin/tunnel-client.exe"
    )
    (root / "bin/tunnel-client.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        components.component_path("tunnel-client", home)


def test_hardlinked_helper_is_rejected(assets, tmp_path):
    import os

    root, _ = assets
    try:
        os.link(root / "bin/tunnel-client.exe", tmp_path / "helper-alias")
    except OSError:
        pytest.skip("Hard links unavailable")
    with pytest.raises(ValueError, match="hardlinked"):
        components.verified_assets(root)


def test_runtime_metadata_does_not_create_owner_state(tmp_path, monkeypatch, capsys):
    home = tmp_path / "must-not-exist"
    monkeypatch.setattr(components, "bundle_root", lambda: tmp_path / "no-bundle")
    assert cli.main(["runtime", "--home", str(home)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["desktop_commander_required"] and not report["node_required"]
    assert report["chatgpt_connection_requires_network_and_account"] is True
    assert not home.exists()


def test_embedded_helper_probe_uses_sanitized_child_environment(assets, monkeypatch):
    root, _ = assets
    monkeypatch.setattr(components, "bundle_root", lambda: root)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "synthetic-do-not-forward")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(components.subprocess, "run", run)
    report = components.runtime_report(probe=True)
    assert report["component_probes"]["tunnel-client"]["ok"]
    assert calls[0][0] == [str(root / "bin/tunnel-client.exe"), "--version"]
    assert "CONTROL_PLANE_API_KEY" not in calls[0][1]["env"]


def test_frozen_mcp_launch_never_uses_a_development_manifest(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    original = b'{"mcpServers":{"development":"this must not be executed"}}'
    (home / "mcp.json").write_bytes(original)
    executable = tmp_path / "udb.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    argv = desktop.mcp_command(home)
    assert argv[:2] == [str(executable), "serve"]
    assert "-m" not in argv
    assert (home / "mcp.json").read_bytes() == original


def test_preparing_a_workspace_creates_no_registration_or_permission(tmp_path):
    home = tmp_path / "new-home"
    workspace = tmp_path / "new-workspace"
    config.prepare_workspace(config.Settings(home=home, project_root=workspace))
    assert home.is_dir() and workspace.is_dir()
    assert not (home / "state").exists()


def test_offline_cli_routes_to_local_agent_without_activating_access(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(owner_command, "main", lambda argv: calls.append(argv) or 0)
    assert cli.main(["start", "--home", str(tmp_path), "--offline"]) == 0
    assert "--offline" in calls[0] and "--access" not in calls[0]


def test_gui_cli_uses_the_same_package_worker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0),
    )
    assert cli.main(["gui", "--home", str(home)]) == 0
    argv, kwargs = calls[0]
    assert "_worker" in argv and "core" in argv and "--gui" in argv
    assert kwargs["shell"] is False
    assert not (home / "state").exists()


def test_gui_cannot_activate_computer_permissions(tmp_path):
    home = tmp_path / "must-not-exist"
    assert cli.main(["--access", "computer", "gui", "--home", str(home)]) != 0
    assert not home.exists()


def test_offline_start_initializes_folders_and_never_requests_a_tunnel(tmp_path):
    home = tmp_path / "fresh-home"
    reports = iter(
        [
            {
                "manager_running": False,
                "local_tunnel_ready": False,
                "connection_mode": "unknown",
            },
            {
                "manager_running": True,
                "local_tunnel_ready": False,
                "connection_mode": "offline",
            },
        ]
    )
    calls = []
    report = owner_command.start(
        home,
        tmp_path / "udb.exe",
        snapshot=lambda *a: next(reports),
        spawn=lambda argv, **kw: calls.append((argv, kw)),
        connect=False,
    )
    assert report["manager_running"] and report["connection_mode"] == "offline"
    assert "--no-connect" in calls[0][0]
    assert home.is_dir() and (home / "projects" / "workspace").is_dir()
    assert not (home / "state" / "computer-access.dpapi").exists()


@pytest.mark.parametrize("frozen", [False, True])
def test_background_manager_owns_its_frozen_runtime(tmp_path, monkeypatch, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "0")
    reports = iter(
        [
            {"manager_running": False, "local_tunnel_ready": False},
            {
                "manager_running": True,
                "local_tunnel_ready": False,
                "connection_mode": "offline",
            },
        ]
    )
    calls = []
    owner_command.start(
        tmp_path / "home",
        tmp_path / "udb.exe",
        snapshot=lambda *a: next(reports),
        spawn=lambda argv, **kw: calls.append(kw),
        connect=False,
    )
    assert calls[0]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == ("1" if frozen else "0")
    import os

    assert os.environ["PYINSTALLER_RESET_ENVIRONMENT"] == "0"


@pytest.mark.parametrize("connect,existing", [(True, "offline"), (False, "online")])
def test_start_does_not_silently_switch_an_existing_manager_mode(
    tmp_path, connect, existing
):
    calls = []
    with pytest.raises(owner_command.CommandError, match="another connection mode"):
        owner_command.start(
            tmp_path,
            tmp_path / "udb.exe",
            connect=connect,
            snapshot=lambda *a: {"manager_running": True, "connection_mode": existing},
            spawn=lambda *a, **kw: calls.append(True),
        )
    assert not calls


def test_offline_heartbeat_is_truthful_and_does_not_issue_access(tmp_path):
    closed = threading.Event()

    def forbidden():
        raise AssertionError("Offline mode attempted a tunnel")

    controller = SimpleNamespace(
        agent=SimpleNamespace(state=tmp_path),
        closed=closed,
        start_host=lambda: None,
        start_tunnel=forbidden,
        status=lambda: {"ready": False, "tunnel_running": False, "connecting": False},
        close=lambda: closed.set(),
    )
    (tmp_path / headless.STOP_NAME).write_text(
        json.dumps({"instance": "a" * 32, "expires": time.time() + 20})
    )
    headless.run_loop(controller, "a" * 32, threading.Event(), connect=False)
    result = json.loads((tmp_path / headless.STATUS_NAME).read_text())
    assert result["connection_mode"] == "offline" and result["phase"] == "stopped"
    assert not (tmp_path / "computer-access.dpapi").exists()


def test_binary_archive_excludes_source_credentials_and_build_logs(
    tmp_path, monkeypatch
):
    base = tmp_path / "checkout"
    destination = tmp_path / "release"
    base.mkdir()
    destination.mkdir()
    (base / "LICENSE").write_text("test license")
    (base / "install.ps1").write_text("# synthetic public installer")
    (destination / "udb.exe").write_bytes(b"synthetic executable")
    (destination / "BUILD.log").write_text("PRIVATE-SENTINEL")
    (destination / "state").mkdir()
    (destination / "state" / "connection.json").write_text("PRIVATE-SENTINEL")
    (destination / "secret.py").write_text("PRIVATE-SENTINEL")
    monkeypatch.setattr(build, "copy_notices", lambda _: [])
    archive = build.standalone_package(base, destination, tmp_path / "no-assets", [])
    with zipfile.ZipFile(archive) as package:
        assert "UDB/udb.exe" in package.namelist()
        assert "UDB/LICENSE" in package.namelist()
        assert "UDB/install.ps1" in package.namelist()
        assert all(
            not name.endswith((".py", ".log", ".dpapi")) for name in package.namelist()
        )
        assert all(
            b"PRIVATE-SENTINEL" not in package.read(name) for name in package.namelist()
        )
