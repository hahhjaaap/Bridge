"""HF 0.3.3 owner-granted autonomy is scoped, logged and revocable."""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from udb import media
from udb.core import Agent, PolicyError, digest


@pytest.fixture
def owner(tmp_path, monkeypatch):
    a = Agent(tmp_path / "bridge")
    root = tmp_path / "sources"
    root.mkdir()
    (root / "hello.py").write_text("v=1\n", encoding="utf8")
    (root / "package.json").write_text('{"name":"keep"}', encoding="utf8")
    (root / "checkpoints").mkdir()
    (root / "checkpoints" / "keep.pt").write_bytes(b"KEEP_BINARY_CHECKPOINT")
    a.register_local("demo", str(root), True)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(media.Path, "home", staticmethod(lambda: tmp_path))
    return a, root, downloads


def test_auto_write_requires_explicit_owner_grant(owner):
    a, root, _ = owner
    sha = a.read("demo", "hello.py")["sha256"]
    request = a.propose_write("demo", "hello.py", "v=2\n", sha)
    assert request["status"] == "pending_local_approval"
    assert (root / "hello.py").read_text("utf8") == "v=1\n"
    a.set_source_autonomy_local("demo", True)
    assert a.source_autonomy_projects() == ["demo"]
    request2 = a.propose_write("demo", "hello.py", "v=3\n", sha)
    assert request2["status"] == "applied_auto"
    assert a.request_status(request2["request_id"])["status"] == "applied"
    assert (root / "hello.py").read_text("utf8") == "v=3\n"
    assert (
        a.state / "backups" / (request2["request_id"] + ".bak")
    ).read_text() == "v=1\n"
    a.set_source_autonomy_local("demo", False)
    assert a.source_autonomy_projects() == []
    later = a.propose_write("demo", "hello.py", "v=4\n", digest(b"v=3\n"))
    assert later["status"] == "pending_local_approval"
    assert (root / "hello.py").read_text("utf8") == "v=3\n"


def test_auto_rejects_critical_and_protected_files(owner):
    a, root, _ = owner
    a.set_source_autonomy_local("demo", True)
    before = (root / "package.json").read_bytes()
    with pytest.raises(PolicyError, match="Important configuration"):
        a.propose_write("demo", "package.json", "{}", digest(before))
    assert (root / "package.json").read_bytes() == before
    with pytest.raises(PolicyError):
        a.propose_write("demo", "checkpoints/keep.pt", "attack", "NEW")
    assert (root / "checkpoints" / "keep.pt").read_bytes() == b"KEEP_BINARY_CHECKPOINT"


def test_wrong_digest_never_overwrites(owner):
    a, root, _ = owner
    a.set_source_autonomy_local("demo", True)
    with pytest.raises(PolicyError, match="Stale"):
        a.propose_write("demo", "hello.py", "v=4\n", "NEW")
    assert (root / "hello.py").read_text() == "v=1\n"


def test_new_file_created_automatically_and_scope_rebinding_denied(owner, tmp_path):
    a, root, _ = owner
    a.set_source_autonomy_local("demo", True)
    new = a.propose_write("demo", "created.py", "print(7)\n", "NEW")
    assert new["status"] == "applied_auto" and new["backup_id"] is None
    assert (root / "created.py").read_text() == "print(7)\n"
    other = tmp_path / "another"
    other.mkdir()
    a.register_local("demo", str(other), True)
    assert not a.source_autonomy_projects()
    request = a.propose_write("demo", "created.py", "v=9\n", "NEW")
    assert request["status"] == "pending_local_approval"
    assert not (other / "created.py").exists()


def test_read_only_project_cannot_get_autonomous_write(owner):
    a, root, _ = owner
    a.register_local("demo", str(root), False)
    with pytest.raises(PolicyError, match="Enable project source writes"):
        a.set_source_autonomy_local("demo", True)


def test_media_queues_only_after_local_grant(owner):
    a, _, downloads = owner
    url = "https://ynvideo.kshunze.com/example/video.m3u8"
    one = media.propose(a, url, "first.mp4")
    assert one["status"] == "pending_local_approval"
    a.set_media_autonomy_local(True)
    two = media.propose(a, url, "second.mp4")
    assert two["status"] == "queued_media"
    assert a.request_status(two["request_id"])["status"] == "queued_media"
    assert not (downloads / "second.mp4").exists()


def test_media_queue_worker_never_bypasses_allowlist(owner, monkeypatch):
    a, _, downloads = owner
    a.set_media_autonomy_local(True)
    calls = []

    def fake_download(agent, rid, payload):
        calls.append(payload["filename"])
        return {"file": str(downloads / payload["filename"]), "bytes": 42}

    monkeypatch.setattr(media, "run_download", fake_download)
    req = media.propose(a, "https://ynvideo.kshunze.com/path/item.m3u8", "item.mp4")
    a.pump_media_queue_local()
    a._media_thread.join(timeout=3)
    status = a.request_status(req["request_id"])
    assert status["status"] == "applied"
    assert calls == ["item.mp4"]
    with pytest.raises(ValueError):
        media.propose(a, "https://other.invalid/video.m3u8", "bad.mp4")
    a.set_media_autonomy_local(False)
    assert not a.media_autonomy()


def test_removed_grant_cancels_queued_media(owner, monkeypatch):
    a, _, _ = owner
    a.set_media_autonomy_local(True)
    req = media.propose(a, "https://ynvideo.kshunze.com/film.m3u8", "film.mp4")
    a.set_media_autonomy_local(False)
    monkeypatch.setattr(media, "run_download", lambda *a: pytest.fail("must not run"))
    a.pump_media_queue_local()
    assert a.request_status(req["request_id"])["status"] == "rejected"


def test_no_unsafe_tool_exposed(owner):
    from udb.server import make_server

    a, _, _ = owner
    names = {t.name for t in __import__("asyncio").run(make_server(a).list_tools())}
    assert "set_source_autonomy_local" not in names
    assert "set_media_autonomy_local" not in names
    assert "run_host_shell" not in names
