"""Fixed guest mailbox correctness and backwards compatibility."""

import hashlib
import json
import time

import pytest

from udb import desktop_guest as guest
from udb import desktop_vm as vm
from udb.core import Agent, canonical
from udb.sandbox import reserve_vm


def test_guest_mailbox_checksum_blocks_partial_invalid_and_duplicate_ids(tmp_path):
    path = tmp_path / "command-mailbox.json"
    payload = {
        "session_id": "a" * 32,
        "request_id": "b" * 32,
        "action": "state",
        "expires": time.time() + 20,
    }
    encoded = canonical(payload).encode("utf8")
    digest = hashlib.sha256(encoded).hexdigest()
    path.write_text(canonical({"payload": payload, "sha256": digest}), encoding="utf8")
    assert guest.read_command_mailbox(path) == payload
    path.write_text('{"payload":', encoding="utf8")
    assert guest.read_command_mailbox(path) is None
    path.write_text(
        canonical({"payload": payload, "sha256": "f" * 64}), encoding="utf8"
    )
    assert guest.read_command_mailbox(path) is None
    path.write_text(
        canonical({"payload": {"request_id": "../private"}, "sha256": digest}),
        encoding="utf8",
    )
    assert guest.read_command_mailbox(path) is None
    assert guest.read_command_mailbox(tmp_path / "missing.json") is None


@pytest.fixture
def desktop(tmp_path):
    agent = Agent(tmp_path / "bridge")
    root = tmp_path / "source"
    root.mkdir()
    (root / "hello.py").write_text('print("demo")\n')
    agent.register_local("demo", str(root), True)
    sid = "c" * 32
    source = agent.state / "desktops" / sid / "input"
    output = source.parent / "output"
    (source / "commands").mkdir(parents=True)
    (output / "replies").mkdir(parents=True)
    (source / "command-mailbox.json").write_text("{}", encoding="utf8")
    info = {
        "kind": "desktop",
        "expires": time.time() + 60,
        "task": "test",
        "permissions": {},
        "project": "demo",
    }
    reserve_vm(agent, sid, info)
    (output / "ready.json").write_text(
        canonical({"session_id": sid, "mailbox_protocol": 1, "state": {}})
    )
    return agent, sid, source, output


def test_host_writes_stable_mailbox_and_verifies_hash(desktop, monkeypatch):
    agent, sid, source, output = desktop
    requests = []

    def simulate_guest(_):
        path = source / "command-mailbox.json"
        payload = guest.read_command_mailbox(path)
        assert payload is not None
        assert payload["session_id"] == sid and payload["action"] == "type"
        requests.append(payload["request_id"])
        (output / "replies" / (payload["request_id"] + ".json")).write_text(
            canonical({"ok": True, "state": {"title": "demo"}})
        )

    monkeypatch.setattr(vm.time, "sleep", simulate_guest)
    assert vm.command(agent, sid, "type", text="guest message")["ok"]
    assert len(requests) == 1
    assert (
        guest.read_command_mailbox(source / "command-mailbox.json")["request_id"]
        == requests[0]
    )
    assert not list((source / "commands").glob("*.json"))
    with agent.connect() as c:
        assert c.execute("SELECT count(*) FROM desktop_control").fetchone()[0] == 0


def test_legacy_guest_without_protocol_stays_with_uuid_files(desktop, monkeypatch):
    agent, sid, source, output = desktop
    (output / "ready.json").write_text(canonical({"session_id": sid, "state": {}}))
    called = []

    def simulate_guest(_):
        paths = list((source / "commands").glob("*.json"))
        assert len(paths) == 1
        called.append(paths[0].name)
        (output / "replies" / paths[0].name).write_text('{"ok":true}')

    monkeypatch.setattr(vm.time, "sleep", simulate_guest)
    assert vm.command(agent, sid, "state")["ok"] is True
    assert len(called) == 1
    assert (source / "command-mailbox.json").read_text() == "{}"
    assert not list((source / "commands").glob("*.json"))
