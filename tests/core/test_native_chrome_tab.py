"""Extension-free tab identity: fail closed on ambiguous UIA observations."""
import json
from pathlib import Path

from udb import chrome_tab_identity as tabs


def row(identity, selected=False):
    return {"id": identity, "selected": selected, "bounds": [20, 0, 200, 41]}


def test_registers_only_new_tab_without_urls_or_names(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(tabs, "observe_tabs", lambda hwnd: [
        row("chrome.older"), row("chrome.new", selected=True)
    ])
    assert tabs.register_new_tab(111, 987, [row("chrome.older")])
    payload = json.loads(tabs.marker_path().read_text("utf-8"))
    assert payload == {
        "version": 1, "tabs": [{"hwnd": 111, "pid": 987, "id": "chrome.new"}]
    }
    assert "http" not in tabs.marker_path().read_text("utf-8")


def test_rejects_ambiguous_or_missing_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(tabs, "observe_tabs", lambda hwnd: [
        row("old"), row("new1"), row("new2")
    ])
    assert not tabs.register_new_tab(111, 987, [row("old")])
    assert not tabs.marker_path().exists()
    assert not tabs.register_new_tab(111, 987, None)
    assert not tabs.register_new_tab(111, 987, [])


def test_retains_only_eight_most_recent_tabs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for i in range(12):
        monkeypatch.setattr(tabs, "observe_tabs", lambda hwnd, i=i: [
            row("old"), row(f"new-{i}")
        ])
        assert tabs.register_new_tab(111, 987, [row("old")])
    records = json.loads(tabs.marker_path().read_text("utf-8"))["tabs"]
    assert len(records) == 8
    assert records[0]["id"] == "new-4"
    assert records[-1]["id"] == "new-11"


def test_marker_and_overlay_do_not_need_extension():
    source = Path(tabs.__file__).with_name("udb_chrome_tab_uia.ps1").read_text("utf-8")
    overlay = Path(tabs.__file__).parent / "companion" / "udb_pointer_overlay.ps1"
    script = overlay.read_text("utf-8")
    assert "ControlType]::TabItem" in source
    assert "GetRuntimeId" in source
    assert "SelectionItemPattern" in script
    assert "GetForegroundWindow" in script
    assert "ai-chrome-tabs.json" in script
    assert "chrome-extension://" not in script
