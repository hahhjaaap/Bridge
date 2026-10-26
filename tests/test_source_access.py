"""Source-folder opt-in regressions using isolated roots and synthetic receipts.

No test issues a real computer grant, reads a user project or modifies owner state.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from udb import cli, source_access
from udb.computer_files import Files
from udb.core import Agent, PolicyError


@pytest.fixture
def source_project(tmp_path, monkeypatch):
    agent = Agent(tmp_path / "owner-home")
    root = tmp_path / "mixed-source-project"
    root.mkdir()
    agent.protect_local(str(root))
    grant = {"id": "a" * 32}
    monkeypatch.setattr(source_access.computer_access, "load", lambda _: grant)
    monkeypatch.setattr(
        source_access.computer_access, "identity", lambda: "fixture-sid"
    )
    monkeypatch.setattr(source_access, "_protect", lambda data: b"fixture:" + data)

    def unprotect(blob):
        if not blob.startswith(b"fixture:"):
            raise ValueError("Invalid synthetic receipt")
        return blob[len(b"fixture:") :]

    monkeypatch.setattr(source_access, "_unprotect", unprotect)
    return agent, root, grant


def issue(source_project, monkeypatch, answer="ALLOW SOURCE\n"):
    agent, root, _ = source_project
    incoming, outgoing = io.StringIO(answer), io.StringIO()
    with monkeypatch.context() as local:
        local.setattr(source_access, "_console", lambda *a, **kw: (incoming, outgoing))
        return source_access.enable_local(agent, str(root))


def test_default_remains_denied_and_confirmation_is_source_only(
    source_project, monkeypatch
):
    agent, root, _ = source_project
    path = root / "module.py"
    path.write_bytes(b"VALUE = 1\n")
    with pytest.raises(PolicyError, match="Protected root"):
        Files(agent).dispatch("read", str(path))
    issue(source_project, monkeypatch)
    assert Files(agent).dispatch("read", str(path))["content"] == "VALUE = 1\n"
    with agent.connect() as connection:
        roots = {row[0] for row in connection.execute("SELECT root FROM protected")}
    assert str(root.resolve()) in roots
    assert not agent.projects()


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "module.py",
        "configs/chat.toml",
        "tools/check.ps1",
        "START.cmd",
        "start.bat",
        "app.tsx",
        ".gitignore",
        "new_module.pyi",
    ],
)
def test_source_read_write_and_listing_work_under_an_owner_receipt(
    source_project, monkeypatch, relative
):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    files = Files(agent)
    written = files.dispatch(
        "write", str(path), content="source text\n", expected_sha256="NEW"
    )
    assert written["bytes"] == 12
    viewed = files.dispatch("read", str(path))
    assert viewed["content"] == "source text\n"
    files.dispatch(
        "write", str(path), content="changed\n", expected_sha256=viewed["sha256"]
    )
    assert files.dispatch("read", str(path))["content"] == "changed\n"
    listing = files.dispatch("list", str(path.parent), limit=100)
    assert path.name in {row["name"] for row in listing["entries"]}


@pytest.mark.parametrize(
    "relative",
    [
        "data/examples.txt",
        "datasets/example.py",
        "training_data/example.py",
        "models/code.py",
        "weights/config.json",
        "checkpoints/README.md",
        "results/run/info.json",
        "outputs/log.md",
        "run/status.json",
        "logs/info.txt",
        ".git/config",
        ".ssh/config",
        ".env",
        ".env.local.py",
        "credentials.json",
        "credential.json",
        "secret.toml",
        "secrets.py",
        "api_key.json",
        "access_token.json",
        "cookie.json",
        "id_rsa.py",
        "checkpoint.pt",
        "weights.safetensors",
        "model.gguf",
        "samples.jsonl",
        "dataset.csv",
        "archive.zip",
        "computer-access.dpapi",
        "unknown.exe",
    ],
)
def test_data_and_credentials_remain_denied(source_project, monkeypatch, relative):
    agent, root, _ = source_project
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic protected fixture")
    issue(source_project, monkeypatch)
    with pytest.raises(PolicyError, match="Protected root"):
        Files(agent).dispatch("read", str(path))
    with pytest.raises(PolicyError, match="Protected root"):
        Files(agent).dispatch("write", str(path), content="no", expected_sha256="NEW")


def test_nested_explicit_protection_requires_its_own_confirmation(
    source_project, monkeypatch
):
    agent, root, _ = source_project
    private = root / "private-source"
    private.mkdir()
    (private / "a.py").write_text("private fixture")
    agent.protect_local(str(private))
    issue(source_project, monkeypatch)
    with pytest.raises(PolicyError, match="Protected root"):
        agent.check_protected(private / "a.py")


def test_copy_and_move_cannot_cross_into_protected_data(source_project, monkeypatch):
    agent, root, _ = source_project
    (root / "a.py").write_text("source")
    (root / "data").mkdir()
    issue(source_project, monkeypatch)
    files = Files(agent)
    sha = files.dispatch("stat", str(root / "a.py"))["sha256"]
    for action in ("copy", "move"):
        with pytest.raises(PolicyError):
            files.dispatch(
                action,
                str(root / "a.py"),
                destination=str(root / "data/a.py"),
                expected_sha256=sha,
            )
        assert (root / "a.py").read_text() == "source"
        assert not (root / "data/a.py").exists()


def test_stale_hash_still_rejects_source_replacement(source_project, monkeypatch):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    path = root / "a.py"
    path.write_text("initial")
    files = Files(agent)
    old = files.dispatch("read", str(path))
    path.write_text("external change")
    with pytest.raises(PolicyError, match="changed"):
        files.dispatch(
            "write", str(path), content="overwrite", expected_sha256=old["sha256"]
        )
    assert path.read_text() == "external change"


def test_cancel_does_not_issue_a_receipt(source_project, monkeypatch):
    agent, root, _ = source_project
    with pytest.raises(PolicyError, match="not enabled"):
        issue(source_project, monkeypatch, "\n")
    assert not source_access.receipt_path(agent, root).exists()


def test_revoke_computer_access_or_new_computer_grant_invalidates_source(
    source_project, monkeypatch
):
    agent, root, grant = source_project
    issue(source_project, monkeypatch)
    assert source_access.load(agent, root)
    grant["id"] = "b" * 32
    assert source_access.load(agent, root) is None
    monkeypatch.setattr(source_access.computer_access, "load", lambda _: None)
    assert source_access.load(agent, root) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "all-files"),
        ("root", "other-root"),
        ("home", "other-home"),
        ("sid", "other-user"),
        ("computer_grant_id", "c" * 32),
        ("policy", 2),
        ("acknowledged_source_only", False),
        ("id", ""),
    ],
)
def test_mismatched_receipt_fails_closed(source_project, monkeypatch, field, value):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    path = source_access.receipt_path(agent, root)
    record = json.loads(source_access._unprotect(path.read_bytes()))
    record[field] = value
    path.write_bytes(source_access._protect(json.dumps(record).encode()))
    assert source_access.load(agent, root) is None
    with pytest.raises(PolicyError):
        agent.check_protected(root / "a.py")


@pytest.mark.parametrize("blob", [b"invalid", b"x" * (16 * 1024 + 1)])
def test_corrupt_or_oversized_receipt_fails_closed(source_project, monkeypatch, blob):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    source_access.receipt_path(agent, root).write_bytes(blob)
    assert source_access.load(agent, root) is None


def test_hardlinked_receipt_is_not_accepted(source_project, monkeypatch, tmp_path):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    path = source_access.receipt_path(agent, root)
    try:
        os.link(path, tmp_path / "receipt-alias")
    except OSError:
        pytest.skip("Hard links unavailable")
    assert source_access.load(agent, root) is None


def test_source_receipt_never_exposes_agent_state_or_installation(
    source_project, monkeypatch
):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    for path in (agent.state / "agent.sqlite3", agent.home / "private.py"):
        with pytest.raises(PolicyError):
            agent.check_protected(path)


def test_owner_can_revoke_one_source_exception(source_project, monkeypatch):
    agent, root, _ = source_project
    issue(source_project, monkeypatch)
    with monkeypatch.context() as local:
        local.setattr(
            source_access,
            "_console",
            lambda *a, **kw: (io.StringIO("REVOKE SOURCE\n"), io.StringIO()),
        )
        source_access.revoke_local(agent, str(root))
    assert source_access.load(agent, root) is None
    with pytest.raises(PolicyError):
        agent.check_protected(root / "a.py")


@pytest.mark.parametrize("action", ["allow-source", "revoke-source"])
def test_noninteractive_cli_cannot_touch_owner_state(tmp_path, action):
    home = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "udb",
            action,
            "--home",
            str(home),
            "--path",
            str(tmp_path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "attended Windows terminal" in result.stderr
    assert not home.exists()


@pytest.mark.parametrize("option", ["--yes", "--force", "--approve"])
def test_no_noninteractive_approval_switch_exists(tmp_path, option):
    with pytest.raises(SystemExit):
        cli.main(["allow-source", "--path", str(tmp_path), option])


def test_json_mode_does_not_issue_source_permission(tmp_path):
    home = tmp_path / "must-not-exist"
    assert (
        cli.main(
            ["--json", "allow-source", "--home", str(home), "--path", str(tmp_path)]
        )
        != 0
    )
    assert not home.exists()


def test_selecting_a_parent_or_data_root_cannot_broaden_the_exception(source_project):
    agent, root, _ = source_project
    (root / "models").mkdir()
    agent.protect_local(str(root / "models"))
    for selected in (root.parent, root / "models", agent.home, agent.state):
        with pytest.raises(PolicyError):
            source_access.selected_root(agent, str(selected))
