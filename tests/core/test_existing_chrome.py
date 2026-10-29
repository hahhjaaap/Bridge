"""Owner-granted visible Chrome tab integration (not CDP or profile takeover)."""

from types import SimpleNamespace

import pytest

from udb.computer_desktop import Desktop
from udb.core import PolicyError

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@pytest.fixture(autouse=True)
def no_live_uia_in_unit_tests(monkeypatch):
    monkeypatch.setattr("udb.chrome_tab_identity.observe_tabs", lambda hwnd: [])
    monkeypatch.setattr(
        "udb.chrome_tab_identity.register_new_tab",
        lambda hwnd, pid, previous: False,
    )


def desktop(windows=None, foreground=101):
    d = Desktop.__new__(Desktop)
    d.interactive = lambda: None
    d.gui = SimpleNamespace(GetForegroundWindow=lambda: foreground)
    d._chrome_windows = lambda: [(101, 9876, CHROME)] if windows is None else windows
    return d


def test_open_tab_reuses_existing_chrome_without_shell_or_user_input(monkeypatch):
    d = desktop()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("udb.computer_desktop.subprocess.run", run)
    result = d._dispatch("open_chrome_tab", text="https://example.org/test")
    assert result["requested"] is True
    assert result["existing_window_reused_observed"] is True
    assert result["chrome_pid"] == 9876
    assert result["dom_attached"] is False
    assert result["user_input_injected"] is False
    assert calls[0][0] == [CHROME, "--new-tab", "https://example.org/test"]
    assert calls[0][1]["shell"] is False
    assert "--new-window" not in calls[0][0]
    assert "--user-data-dir" not in calls[0][0]


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret.txt",
        "http://localhost:9222/json",
        "http://127.0.0.1:9222",
        "https://user:password@example.org/",
        "javascript:alert(1)",
        "https://example.org/\n--new-window",
    ],
)
def test_open_tab_rejects_unsafe_urls_before_process_launch(monkeypatch, url):
    d = desktop()
    monkeypatch.setattr(
        "udb.computer_desktop.subprocess.run",
        lambda *a, **kw: pytest.fail("Process launch must not happen"),
    )
    with pytest.raises(PolicyError):
        d.open_chrome_tab(url)


def test_open_tab_requires_an_existing_chrome_window(monkeypatch):
    d = desktop(windows=[])
    monkeypatch.setattr(
        "udb.computer_desktop.subprocess.run",
        lambda *a, **kw: pytest.fail("Must not create a Chrome window"),
    )
    with pytest.raises(PolicyError, match="No existing Google Chrome"):
        d.open_chrome_tab("https://example.org/")


def test_open_tab_reports_new_window_rather_than_claiming_reuse(monkeypatch):
    d = desktop()
    before = [(101, 9876, CHROME)]
    after = before + [(202, 9876, CHROME)]
    calls = iter([before, after])
    d._chrome_windows = lambda: next(calls)
    monkeypatch.setattr(
        "udb.computer_desktop.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0),
    )
    result = d.open_chrome_tab("https://example.org/")
    assert result["existing_window_reused_observed"] is False
    assert result["chrome_windows_before"] == 1
    assert result["chrome_windows_after"] == 2


def test_open_tab_failed_launch_requires_inspection_before_retry(monkeypatch):
    d = desktop()
    monkeypatch.setattr(
        "udb.computer_desktop.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=1),
    )
    with pytest.raises(PolicyError, match="inspect before retrying"):
        d.open_chrome_tab("https://example.org/")


def test_open_tab_is_not_allowed_inside_desktop_batch():
    d = desktop()
    with pytest.raises(PolicyError, match="Invalid/nested batch step"):
        d.batch(
            "some-window",
            [{"action": "open_chrome_tab", "text": "https://example.org"}],
        )


def test_open_tab_is_a_recognized_desktop_action():
    Desktop.validate_options("open_chrome_tab")


def test_open_tab_uses_opted_in_chrome_aura_launcher(monkeypatch, tmp_path):
    from udb.chrome_aura import EXTENSION_ID

    marker = tmp_path / "UniversalDesktopBridge" / "chrome-aura-enabled"
    marker.parent.mkdir()
    marker.write_text(EXTENSION_ID, encoding="ascii")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = desktop()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("udb.computer_desktop.subprocess.run", run)
    result = d.open_chrome_tab("https://example.org/ai?test=1")
    assert result["branding_requested"] is True
    assert result["branding_verified"] is False
    assert calls[0][0] == CHROME
    assert calls[0][1] == "--new-tab"
    assert calls[0][2].startswith(f"chrome-extension://{EXTENSION_ID}/launch.html#")
    assert "https%3A%2F%2Fexample.org%2Fai%3Ftest%3D1" in calls[0][2]
