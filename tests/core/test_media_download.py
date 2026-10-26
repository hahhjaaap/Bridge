"""No-network tests for locally approved HLS -> Downloads capability."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import components, media
from udb._version import VERSION
from udb.core import Agent


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from udb import resources

    monkeypatch.setattr(resources, "guard_new_heavy_task", lambda *a: None)
    a = Agent(tmp_path / "bridge")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(media.Path, "home", staticmethod(lambda: tmp_path))
    return a, downloads


URL = "https://ynvideo.kshunze.com/dfyx_scrm/DFYX363/DFYX363.m3u8"


def test_media_user_agent_tracks_package_version():
    assert media.USER_AGENT == f"UniversalDesktopBridge/{VERSION}"


def test_media_request_requires_owner_approval(setup):
    agent, downloads = setup
    p = media.propose(agent, URL, "new_video.mp4")
    assert p["status"] == "pending_local_approval"
    assert agent.request_status(p["request_id"])["status"] == "pending"
    assert not (downloads / "new_video.mp4").exists()
    assert agent.diagnostics()["host_execution"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://ynvideo.kshunze.com/sample.m3u8",
        "https://localhost/private.m3u8",
        "https://127.0.0.1/private.m3u8",
        "https://evil.test/video.m3u8",
        "file:///etc/passwd",
        "https://ynvideo.kshunze.com/a.exe",
        "https://user:pass@ynvideo.kshunze.com/x.m3u8",
        "https://ynvideo.kshunze.com:444/x.m3u8",
    ],
)
def test_invalid_media_url_rejected(setup, url):
    agent, _ = setup
    with pytest.raises((ValueError, TypeError)):
        media.propose(agent, url, "valid.mp4")


@pytest.mark.parametrize(
    "name",
    [
        "../outside.mp4",
        "evil.txt",
        "x/y.mp4",
        "C:danger.mp4",
        ".mp4",
        " video.mp4",
        "CON?.mp4",
    ],
)
def test_bad_names_rejected(setup, name):
    agent, _ = setup
    with pytest.raises(ValueError):
        media.propose(agent, URL, name)


def test_no_overwrite(setup):
    agent, downloads = setup
    (downloads / "video.mp4").write_bytes(b"KEEP")
    with pytest.raises(FileExistsError):
        media.propose(agent, URL, "video.mp4")
    assert (downloads / "video.mp4").read_bytes() == b"KEEP"


def test_local_approval_runs_fixed_ffmpeg_without_shell(setup, monkeypatch):
    agent, downloads = setup
    commands = []
    monkeypatch.setattr(media, "_manifest", lambda *args: (10.0, 1))
    monkeypatch.setattr(components, "component_path", lambda tool, home=None: tool)

    def fake_run(argv, **kwargs):
        commands.append((argv, kwargs))
        assert isinstance(argv, list) and "shell" not in kwargs
        assert kwargs["stdin"] == subprocess.DEVNULL
        if argv[0] == "ffmpeg":
            assert (
                argv[argv.index("-protocol_whitelist") + 1]
                == "http,https,tcp,tls,crypto"
            )
            assert argv[argv.index("-fs") + 1] == str(media.MAX_BYTES)
            Path(argv[-1]).write_bytes(b"FAKE MP4 OUTPUT")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {
                        "duration": "10.03",
                        "size": str(len(b"FAKE MP4 OUTPUT")),
                    },
                    "streams": [{"codec_type": "video"}],
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    request = media.propose(agent, URL, "verified.mp4")
    result = agent.decide_local(request["request_id"], True)
    assert len(commands) == 2
    assert result["file"] == str(downloads / "verified.mp4")
    assert (downloads / "verified.mp4").read_bytes() == b"FAKE MP4 OUTPUT"
    assert agent.request_status(request["request_id"])["status"] == "applied"
    assert not any(downloads.glob(".udb-*.mp4"))


def test_rejection_never_executes_media(setup, monkeypatch):
    agent, downloads = setup
    monkeypatch.setattr(
        media,
        "run_download",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    request = media.propose(agent, URL, "reject.mp4")
    agent.decide_local(request["request_id"], False)
    assert agent.request_status(request["request_id"])["status"] == "rejected"
    assert not (downloads / "reject.mp4").exists()
