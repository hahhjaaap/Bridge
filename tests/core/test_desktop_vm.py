import json
import os
import struct
import time
import zlib

import pytest

from udb import desktop_vm as vm
from udb.core import Agent, PolicyError
from udb.desktop_guest import assert_guest
from udb.sandbox import reserve_vm


@pytest.fixture
def desktop(tmp_path):
    agent = Agent(tmp_path / "bridge")
    root = tmp_path / "source"
    root.mkdir()
    (root / "hello.py").write_text("x=1\n")
    agent.register_local("demo", str(root), True)
    sid = "c" * 32
    source = agent.state / "desktops" / sid / "input"
    output = source.parent / "output"
    (source / "commands").mkdir(parents=True)
    (output / "replies").mkdir(parents=True)
    info = {
        "kind": "desktop",
        "expires": time.time() + 60,
        "task": "Test guest only",
        "permissions": {},
        "project": "demo",
    }
    reserve_vm(agent, sid, info)
    (output / "ready.json").write_text(
        json.dumps({"session_id": sid, "state": {"width": 800, "height": 600}})
    )
    return agent, sid, source, output


def test_proposal_never_grants_desktop(tmp_path, monkeypatch):
    agent = Agent(tmp_path / "bridge")
    root = tmp_path / "source"
    root.mkdir()
    agent.register_local("demo", str(root), True)
    monkeypatch.setattr(vm, "available", lambda: True)
    proposal = agent.propose_desktop("demo", "Type a demo message", 600)
    assert agent.request_status(proposal["request_id"])["status"] == "pending"
    with pytest.raises(PolicyError, match="Unknown desktop"):
        vm.command(agent, proposal["request_id"], "type", text="must not type")


def test_action_delivered_only_to_scoped_guest_mailbox(desktop, monkeypatch):
    agent, sid, source, output = desktop

    def reply(_):
        for p in (source / "commands").glob("*.json"):
            request = json.loads(p.read_text())
            assert request["session_id"] == sid and request["text"] == "hello guest"
            (output / "replies" / p.name).write_text(
                json.dumps({"ok": True, "state": {"active_title": "demo"}})
            )

    monkeypatch.setattr(vm.time, "sleep", reply)
    assert vm.command(agent, sid, "type", text="hello guest")["ok"]
    assert not list((source / "commands").glob("*.json"))
    with agent.connect() as c:
        assert c.execute("SELECT count(*) FROM desktop_control").fetchone()[0] == 0


def test_closed_or_expired_desktop_refuses_input(desktop):
    agent, sid, _, _ = desktop
    vm.stop(agent, sid)
    with pytest.raises(PolicyError, match="closed or expired"):
        vm.command(agent, sid, "click", x=5, y=5)
    with pytest.raises(PolicyError, match="One Sandbox"):
        reserve_vm(agent, "d" * 32, {"kind": "desktop"})


def test_gui_close_commits_stopped_without_daemon_monitor(desktop, monkeypatch):
    agent, sid, _, _ = desktop

    class OwnedVM:
        code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = 1

        def wait(self, timeout):
            assert self.code is not None
            return self.code

    proc = OwnedVM()
    monkeypatch.setattr(vm, "_OWNED", {sid: (agent, proc)})
    vm.close_owned(agent)
    assert vm.session(agent, sid)["status"] == "stopped" and not vm._OWNED
    reserve_vm(agent, "d" * 32, {"kind": "desktop"})


def test_parallel_action_lock_is_enforced(desktop):
    agent, sid, _, _ = desktop
    with agent.connect() as c:
        c.execute(
            "CREATE TABLE desktop_control(session TEXT PRIMARY KEY,request_id TEXT,expires REAL)"
        )
        c.execute(
            "INSERT INTO desktop_control VALUES (?,?,?)",
            (sid, "e" * 32, time.time() + 30),
        )
    with pytest.raises(PolicyError, match="Another desktop action"):
        vm.command(agent, sid, "state")


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "shell"},
        {"action": "click", "x": -1},
        {"action": "click", "clicks": 50},
        {"action": "type", "text": "x" * 2001},
        {"action": "scroll", "delta": 1201},
    ],
)
def test_invalid_actions_fail_before_mailbox(arguments):
    with pytest.raises(PolicyError):
        vm.validate_action(**arguments)


def test_ids_cannot_escape_state(desktop):
    with pytest.raises(PolicyError, match="Invalid desktop"):
        vm.session(desktop[0], "../state")


def test_worker_refuses_this_host():
    with pytest.raises(RuntimeError, match="Sandbox|guest"):
        assert_guest(
            {"guest_only": True, "host_name": "this-host", "session_id": "f" * 32}
        )


def test_output_open_handle_checked_after_link_check(tmp_path, monkeypatch):
    target = tmp_path / "private.txt"
    target.write_bytes(b"PRIVATE")
    link = tmp_path / "guest.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    monkeypatch.setattr(vm, "reject_links", lambda _: None)
    with pytest.raises(PolicyError, match="redirected"):
        vm.safe_bytes(link, 100)


def test_guest_png_crc_and_dimensions():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )
    vm.validate_png(png)
    with pytest.raises(PolicyError):
        vm.validate_png(png[:-1] + b"x")
    with pytest.raises(PolicyError):
        vm.validate_png(b"not a screenshot")


def test_no_original_project_or_state_mapping(tmp_path):
    import xml.etree.ElementTree as ET

    xml = ET.fromstring(
        vm.build_config(
            tmp_path / "session-input", tmp_path / "session-output", tmp_path / "Python"
        )
    )
    maps = xml.findall("./MappedFolders/MappedFolder")
    assert len(maps) == 3 and [m.findtext("ReadOnly") for m in maps] == [
        "true",
        "false",
        "true",
    ]
    assert (
        xml.findtext("Networking") == "Disable"
        and xml.findtext("ClipboardRedirection") == "Disable"
    )
    assert "guest.py" not in xml.findtext(
        "./LogonCommand/Command"
    )  # trusted guest bootstrap only
