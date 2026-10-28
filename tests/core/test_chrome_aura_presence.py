"""Chrome Aura opt-in and automatic owner-granted AI cursor presence."""

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb.chrome_aura import EXTENSION_ID, launch_target
from udb.companion.virtual_pointer import VirtualPointer
from udb.proxy import UnifiedProxy


def test_chrome_aura_is_disabled_by_default(tmp_path):
    assert launch_target("https://example.org/", tmp_path) == (
        "https://example.org/",
        False,
    )


def test_chrome_aura_requires_exact_local_opt_in(tmp_path):
    marker = tmp_path / "UniversalDesktopBridge" / "chrome-aura-enabled"
    marker.parent.mkdir()
    marker.write_text("untrusted-extension-id\n", encoding="ascii")
    assert not launch_target("https://example.org/", tmp_path)[1]
    marker.write_text(EXTENSION_ID + "\n", encoding="ascii")
    target, enabled = launch_target("https://example.org/a?x=1#part", tmp_path)
    assert enabled
    assert target.startswith(f"chrome-extension://{EXTENSION_ID}/launch.html#")
    assert "https%3A%2F%2Fexample.org%2Fa%3Fx%3D1%23part" in target
    marker.write_text("a" * 1024, encoding="ascii")
    assert not launch_target("https://example.org/", tmp_path)[1]


def test_chrome_aura_manifest_has_stable_id_and_scoped_injection():
    root = Path(__file__).resolve().parents[2] / "chrome-extension" / "udb-aura"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    public_key = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    expected = "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
    assert expected == EXTENSION_ID
    assert manifest["manifest_version"] == 3
    assert "web_accessible_resources" not in manifest
    assert "nativeMessaging" not in manifest["permissions"]
    for file in ("launch.html", "launch.js", "background.js", "aura.js", "popup.html", "popup.js"):
        assert (root / file).is_file()
    for size in (16, 32, 48, 128):
        assert (root / "icons" / f"icon{size}.png").is_file()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_chrome_aura_javascript_syntax():
    root = Path(__file__).resolve().parents[2] / "chrome-extension" / "udb-aura"
    node = shutil.which("node")
    assert node is not None
    for filename in ("background.js", "launch.js", "popup.js", "aura.js"):
        subprocess.run(
            [node, "--check", str(root / filename)],
            check=True,
            capture_output=True,
            timeout=15,
        )


def test_virtual_pointer_presence_uses_virtual_screen_not_system_cursor():
    pointer = VirtualPointer.__new__(VirtualPointer)
    pointer.interactive = lambda: None
    metrics = {76: -1920, 77: -200, 78: 3840, 79: 1200}
    pointer.u = SimpleNamespace(GetSystemMetrics=lambda i: metrics[i])
    points = []
    pointer.show = lambda x, y: (points.append((x, y)), setattr(pointer, "position", [x, y]))
    assert pointer.show_idle() == {
        "visibility": "selected_udb_chrome_tab_only",
        "position": [691, 616],
        "independent": True,
    }
    assert points == [(691, 616)]


class OneOrTwoCycles:
    def __init__(self, cycles):
        self.cycles = cycles
        self.count = 0

    def wait(self, seconds):
        self.count += 1
        return self.count > self.cycles


class FakePrimary:
    def __init__(self, states):
        self.states = iter(states)

    def request(self, method, params, timeout=5):
        assert method == "tools/call"
        assert params["name"] == "computer_status"
        return {"result": {"structuredContent": next(self.states)}}


class FakeCompanion:
    def __init__(self):
        self.calls = []

    def request(self, method, timeout=10):
        self.calls.append(method)
        return {"result": {"visible": True}}


def test_proxy_auto_shows_pointer_once_after_existing_desktop_grant():
    grant = {"enabled": True, "capabilities": ["desktop"]}
    primary = FakePrimary([grant, grant])
    companion = FakeCompanion()
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = OneOrTwoCycles(2)
    proxy._watch_access()
    assert companion.calls == ["udb/show-pointer"]
    assert proxy._pointer_presence_started


def test_proxy_hides_pointer_when_grant_is_revoked():
    primary = FakePrimary(
        [
            {"enabled": True, "capabilities": ["desktop"]},
            {"enabled": False, "capabilities": []},
        ]
    )
    companion = FakeCompanion()
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = OneOrTwoCycles(2)
    proxy._watch_access()
    assert companion.calls == ["udb/show-pointer", "udb/close-sessions"]
    assert not proxy._pointer_presence_started


def test_proxy_never_starts_pointer_without_desktop_capability():
    primary = FakePrimary([{"enabled": True, "capabilities": ["files"]}])
    companion = FakeCompanion()
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = OneOrTwoCycles(1)
    proxy._watch_access()
    assert companion.calls == ["udb/close-desktop"]


def test_optional_pointer_failure_does_not_close_existing_work():
    class FailedCompanion(FakeCompanion):
        def request(self, method, timeout=10):
            self.calls.append(method)
            if method == "udb/show-pointer":
                raise RuntimeError("Overlay unavailable")
            return {"result": {}}

    primary = FakePrimary([{"enabled": True, "capabilities": ["desktop"]}])
    companion = FailedCompanion()
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = OneOrTwoCycles(1)
    proxy._watch_access()
    assert companion.calls == ["udb/show-pointer"]
    assert not proxy._pointer_presence_started


def test_standalone_package_includes_opt_in_chrome_aura(tmp_path, monkeypatch):
    import zipfile

    from udb import build

    root = Path(__file__).resolve().parents[2]
    (tmp_path / "udb.exe").write_bytes(b"synthetic executable")
    monkeypatch.setattr(build, "copy_notices", lambda _: [])
    archive = build.standalone_package(root, tmp_path, tmp_path, [])
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
    assert "UDB/chrome-extension/udb-aura/manifest.json" in names
    assert "UDB/chrome-extension/udb-aura/background.js" in names
    assert "UDB/chrome-extension/udb-aura/icons/icon128.png" in names
    assert "UDB/tools/enable_chrome_aura.ps1" in names
    assert "UDB/STANDALONE-SHA256SUMS" in names
