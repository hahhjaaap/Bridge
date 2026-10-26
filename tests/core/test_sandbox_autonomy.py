import json
from pathlib import Path

import pytest

from udb import desktop_vm, sandbox
from udb.core import Agent, PolicyError, canonical


@pytest.fixture
def owner(tmp_path, monkeypatch):
    a = Agent(tmp_path / "bridge")
    root = tmp_path / "source"
    root.mkdir()
    (root / "hello.py").write_text("x=1\n")
    a.register_local("demo", str(root), True)
    monkeypatch.setattr(desktop_vm, "available", lambda: True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    return a, root


def test_grant_dispatches_guest_but_never_host_write(owner, monkeypatch):
    a, root = owner
    calls = []
    a.set_sandbox_autonomy_local("demo", True)
    monkeypatch.setattr(
        desktop_vm,
        "launch",
        lambda agent, sid, p: calls.append((sid, p)) or {"session_id": sid},
    )
    proposal = a.propose_desktop("demo", "Work on the source snapshot", 600)
    assert proposal["status"] == "queued_sandbox" and not calls
    write = a.propose_write(
        "demo", "hello.py", "x=2\n", a.read("demo", "hello.py")["sha256"]
    )
    assert write["status"] == "pending_local_approval"
    a.pump_sandbox_queue_local()
    a.pump_sandbox_queue_local()
    assert len(calls) == 1 and calls[0][1]["authorization"]
    assert a.request_status(proposal["request_id"])["status"] == "started"
    assert (root / "hello.py").read_text() == "x=1\n"
    assert a.request_status(write["request_id"])["status"] == "pending"


def test_default_requires_owner_and_new_grant_covers_existing_guest_only(owner):
    a, _ = owner
    desktop = a.propose_desktop("demo", "Demo", 600)
    write = a.propose_write("demo", "new.py", "x=1\n", "NEW")
    assert desktop["status"] == "pending_local_approval"
    a.set_sandbox_autonomy_local("demo", True)
    assert a.request_status(desktop["request_id"])["status"] == "queued_sandbox"
    assert a.request_status(write["request_id"])["status"] == "pending"


def test_root_rebinding_revokes_effective_grant(owner, tmp_path, monkeypatch):
    a, _ = owner
    a.set_sandbox_autonomy_local("demo", True)
    proposal = a.propose_desktop("demo", "Old source", 600)
    newroot = tmp_path / "other"
    newroot.mkdir()
    a.register_local("demo", str(newroot), True)
    monkeypatch.setattr(
        desktop_vm,
        "launch",
        lambda *args: pytest.fail("Must not launch a rebound project"),
    )
    assert not a.sandbox_autonomy_projects()
    a.pump_sandbox_queue_local()
    assert a.request_status(proposal["request_id"])["status"] == "failed"
    assert (
        a.propose_desktop("demo", "New scope", 600)["status"]
        == "pending_local_approval"
    )


def test_revocation_cancels_queue_and_marks_owned_sessions_stopping(owner):
    a, root = owner
    a.set_sandbox_autonomy_local("demo", True)
    proposal = a.propose_desktop("demo", "Demo", 600)
    with a.connect() as c:
        c.execute(
            "INSERT INTO jobs VALUES (?,?,?)",
            (
                "c" * 32,
                "running",
                canonical({"project": "demo", "authorization": "old-grant"}),
            ),
        )
    a.set_sandbox_autonomy_local("demo", False)
    assert not a.sandbox_authorization("demo", str(root))
    assert a.request_status(proposal["request_id"])["status"] == "rejected"
    with a.connect() as c:
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "stopping"


def test_protected_data_stays_outside_auto_snapshot(owner):
    a, root = owner
    (root / "checkpoints").mkdir()
    sentinel = root / "checkpoints" / "keep.pt"
    sentinel.write_bytes(b"KEEP")
    a.set_sandbox_autonomy_local("demo", True)
    proposal = a.propose_desktop("demo", "Work", 600)
    with a.connect() as c:
        p = json.loads(
            c.execute(
                "SELECT payload FROM requests WHERE id=?", (proposal["request_id"],)
            ).fetchone()[0]
        )
    assert "checkpoints/keep.pt" not in p["manifest"]
    with pytest.raises(PolicyError):
        a.read_bytes("demo", "checkpoints/keep.pt")
    assert sentinel.read_bytes() == b"KEEP"


def test_payload_tampering_and_write_queue_cannot_use_grant(owner, monkeypatch):
    a, _ = owner
    a.set_sandbox_autonomy_local("demo", True)
    proposal = a.propose_desktop("demo", "Demo", 600)
    with a.connect() as c:
        c.execute(
            "UPDATE requests SET kind='write' WHERE id=?", (proposal["request_id"],)
        )
    monkeypatch.setattr(
        desktop_vm,
        "launch",
        lambda *args: pytest.fail("Must not launch tampered request"),
    )
    a.pump_sandbox_queue_local()
    assert a.request_status(proposal["request_id"])["status"] == "failed"


def test_jobs_use_same_guest_only_grant(owner, monkeypatch):
    a, _ = owner
    a.set_sandbox_autonomy_local("demo", True)
    calls = []
    monkeypatch.setattr(
        a, "start_job", lambda sid, p: calls.append(p) or {"job_id": sid}
    )
    r = a.propose_job("demo", "python", "print(5)", 10)
    assert r["status"] == "queued_sandbox"
    a.pump_sandbox_queue_local()
    assert len(calls) == 1 and calls[0]["authorization"]
