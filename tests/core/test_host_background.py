"""Host isolation, resource admission, bounded sessions and conservative failures."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb.core import Agent, PolicyError
from udb.host_browser import Browser, parse_key, validate_locator, validate_url
from udb.host_client import atomic_json, status
from udb.host_service import HostService
from udb.resources import LIMITS, Governor, require_capacity


def sample(**kwargs):
    return dict(
        at=time.time(),
        cpu_percent=20,
        ram_percent=40,
        available_mib=6000,
        commit_percent=50,
        **kwargs,
    )


def test_cpu_warmup_never_admits_without_a_delta():
    s = sample()
    s["cpu_percent"] = None
    assert not Governor().update(s)["admission_ready"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("cpu_percent", 85),
        ("ram_percent", 85),
        ("available_mib", 1535),
        ("commit_percent", 90),
    ],
)
def test_pressure_pauses_and_needs_three_cool_samples(key, value):
    g = Governor()
    s = sample()
    s[key] = value
    assert g.update(s)["paused"]
    assert not g.update(sample())["admission_ready"]
    assert not g.update(sample())["admission_ready"]
    assert g.update(sample())["admission_ready"]


def test_recovery_hysteresis_resets_when_machine_busy_again():
    g = Governor()
    s = sample()
    s["ram_percent"] = 90
    g.update(s)
    g.update(sample())
    g.update(sample())
    s["ram_percent"] = 79
    assert g.update(s)["paused"]
    assert not g.update(sample())["admission_ready"]


def test_stale_unknown_and_reserved_ram_reject_without_launch():
    g = Governor()
    s = g.update(sample())
    require_capacity(s, 512)
    s["at"] = time.time() - 7
    with pytest.raises(PolicyError):
        require_capacity(s)
    s["at"] = time.time()
    s["available_mib"] = 1600
    with pytest.raises(PolicyError):
        require_capacity(s, 512)


def test_heavy_task_uses_live_governor_and_preserves_reserve(monkeypatch):
    from udb import host_client, resources

    monkeypatch.setattr(resources, "os", SimpleNamespace(name="nt"))
    current = Governor().update(sample())
    current["available_mib"] = 2000
    monkeypatch.setattr(
        host_client, "status", lambda a: dict(worker_ready=True, resources=current)
    )
    with pytest.raises(PolicyError, match="RAM"):
        resources.guard_new_heavy_task(object(), 512)
    current["available_mib"] = 7000
    resources.guard_new_heavy_task(object(), 4096)
    current["admission_ready"] = False
    with pytest.raises(PolicyError):
        resources.guard_new_heavy_task(object(), 256)


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret.txt",
        "javascript:alert(1)",
        "data:text/html,x",
        "chrome://settings",
        "http://localhost",
        "http://127.0.0.1:80",
        "http://[::1]",
        "http://192.168.0.1",
        "https://u:p@example.com",
        "https://example.com/\n",
    ],
)
def test_browser_rejects_local_file_internal_and_credential_urls(url):
    with pytest.raises(PolicyError):
        validate_url(url)


def test_public_url_and_blank_are_supported():
    assert validate_url("https://example.com/test?q=1")
    assert validate_url("about:blank")


def test_foreign_tab_cannot_be_addressed():
    b = Browser.__new__(Browser)
    b.tabs = {"owned": "sid"}
    with pytest.raises(PolicyError):
        b.sid("user-tab")


@pytest.fixture
def engine(tmp_path):
    a = Agent(tmp_path / "home")
    atomic_json(a.state / "host-settings.json", {"enabled": True})
    h = HostService.__new__(HostService)
    h.agent = a
    h.sessions = {}
    h.resource = Governor().update(sample())
    h.folder = a.state / "host"
    h.folder.mkdir()
    h.current = None
    return h


def test_disabled_mode_cannot_start_but_owned_close_still_works(engine):
    atomic_json(engine.agent.state / "host-settings.json", {"enabled": False})
    with pytest.raises(PolicyError):
        engine.dispatch("start", {"kind": "browser", "task": "test"})
    assert engine.dispatch("close", {"session_id": "a" * 32})["closed"] is False


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "powershell", "task": "x"},
        {"kind": "browser", "task": ""},
        {"kind": "browser", "task": "x", "duration_seconds": 99999},
    ],
)
def test_no_arbitrary_app_or_unbounded_session(engine, args):
    with pytest.raises(PolicyError):
        engine.dispatch("start", args)


def test_pressure_blocks_open_and_allows_owned_cleanup(engine):
    engine.resource["admission_ready"] = False
    with pytest.raises(PolicyError):
        engine.dispatch("start", {"kind": "browser", "task": "test"})
    assert engine.dispatch("close", {"session_id": "a" * 32})["closed"] is False


def test_single_browser_and_max_two_sessions(engine):
    engine.sessions = {"a": {"kind": "browser"}, "b": {"kind": "workspace"}}
    with pytest.raises(PolicyError, match="Maximum 2"):
        engine.dispatch("start", {"kind": "browser", "task": "test"})
    engine.sessions.pop("b")
    with pytest.raises(PolicyError, match="Reuse"):
        engine.dispatch("start", {"kind": "browser", "task": "test"})


def test_tab_cap_prevents_new_target_creation():
    b = Browser.__new__(Browser)
    b.listing = lambda: [{}] * 4
    b.call = lambda *a, **k: pytest.fail("must reject before browser side effect")
    with pytest.raises(PolicyError, match="Maximum 4"):
        b.new("https://example.com")


def test_stale_service_snapshot_not_accepted(tmp_path):
    a = Agent(tmp_path / "home")
    (a.state / "host").mkdir()
    atomic_json(
        a.state / "host/status.json",
        {"worker_ready": True, "resources": {"at": time.time() - 8}},
    )
    assert not status(a)["worker_ready"]


def test_workspace_has_no_file_or_system_input_capability(engine):
    engine.sessions = {
        "a" * 32: {"kind": "workspace", "expires": time.time() + 60, "object": object()}
    }
    for action in ("shell", "click", "keys", "save", "focus"):
        with pytest.raises(PolicyError):
            engine.dispatch("workspace", {"session_id": "a" * 32, "action": action})


def test_expired_command_is_not_replayed(engine):
    h = engine
    h.folder.mkdir(exist_ok=True)
    for name in ("inbox", "replies"):
        (h.folder / name).mkdir()
    h.stop_event = threading.Event()
    h.maintenance = threading.Event()
    h.lock = threading.RLock()
    h.publish = lambda: None
    h.dispatch = lambda *a: pytest.fail("Expired write must never be replayed")
    rid = "b" * 32
    atomic_json(
        h.folder / "inbox" / (rid + ".json"),
        dict(id=rid, expires=time.time() - 1, op="workspace", args={}),
    )
    t = threading.Thread(target=h._work)
    t.start()
    try:
        end = time.time() + 2
        p = h.folder / "replies" / (rid + ".json")
        while not p.exists() and time.time() < end:
            time.sleep(0.01)
        result = json.loads(p.read_text())
        assert not result["ok"] and "expired" in result["error"]
    finally:
        h.stop_event.set()
        t.join(2)


def test_background_checkpoint_persists_only_dirty_tasks():
    from udb.companion.background import Background, Task

    task = Task("fixture")
    writes = []

    def persist():
        with task.lock:
            writes.append(task._checkpoint_version)
            task._persisted_checkpoint_version = task._checkpoint_version

    task.persist = persist
    background = Background.__new__(Background)
    background.tasks = {task.id: task}

    background.checkpoint()
    background.checkpoint()
    assert writes == [0]

    with task.lock:
        task._checkpoint_version += 1
    background.checkpoint()
    background.checkpoint()
    assert writes == [0, 1]

    task.stop()
    background.checkpoint()
    background.checkpoint()
    assert writes == [0, 1, 2]


def test_task_terminal_state_changes_mark_checkpoint_dirty():
    from udb.companion.background import Task

    task = Task("fixture")
    snapshots = []
    task.persist = lambda: snapshots.append(
        (task.state, task.committed, task._checkpoint_version)
    )
    task.start(lambda _: {"ok": True})
    task.thread.join(timeout=2)
    assert not task.thread.is_alive()
    assert snapshots[-1] == ("completed", False, 1)

    committed = Task("fixture")
    commit_snapshots = []
    committed.persist = lambda: commit_snapshots.append(
        (committed.state, committed.committed, committed._checkpoint_version)
    )
    committed.commit(lambda: None)
    assert commit_snapshots == [
        ("running", False, 0),
        ("running", True, 1),
    ]


@pytest.mark.parametrize(
    "selector,role,name",
    [
        ("#save", "", ""), ("", "button", "Save"), ("", "textbox", "Email"),
        ("", "link", "Continue"), ("", "checkbox", "Agree"),
        ("", "combobox", "Country"), ("", "heading", "Results"),
    ],
)
def test_browser_locator_accepts_css_or_semantic_role(selector, role, name):
    assert validate_locator(selector, role, name) is None


@pytest.mark.parametrize(
    "selector,role,name",
    [
        ("#save", "button", "Save"), ("", "", ""), ("", "", "Save"),
        ("", "document", "Page"), ("x" * 501, "", ""),
    ],
)
def test_browser_locator_rejects_ambiguous_or_unsupported_locators(selector, role, name):
    with pytest.raises(PolicyError):
        validate_locator(selector, role, name)


def test_browser_semantic_click_uses_accessible_role_and_name():
    browser = Browser.__new__(Browser)
    captured = {}
    browser.evaluate = lambda tab, expression, timeout=10: captured.update(
        tab=tab, expression=expression, timeout=timeout
    ) or {"ok": True}
    result = browser.element("owned-tab", "click", role="button", name="Save")
    assert result["ok"]
    assert captured["tab"] == "owned-tab"
    assert '"button"' in captured["expression"]
    assert '"Save"' in captured["expression"]
    assert "locator_not_unique" in captured["expression"]


def test_browser_snapshot_is_bounded_and_redacts_secret_controls():
    browser = Browser.__new__(Browser)
    captured = {}
    browser.evaluate = lambda tab, expression, timeout=10: captured.update(
        tab=tab, expression=expression, timeout=timeout
    ) or {"count": 0, "elements": [], "truncated": False}
    result = browser.snapshot("owned-tab")
    assert result["count"] == 0
    assert "slice(0,160)" in captured["expression"]
    assert "'[redacted]'" in captured["expression"]
    assert "aria-labelledby" in captured["expression"]


def test_browser_wait_for_polls_until_visible():
    browser = Browser.__new__(Browser)
    results = iter([{"ok": False, "state": "visible", "matches": 0},
                    {"ok": True, "state": "visible", "matches": 1, "visible": 1}])
    captured = []
    browser.evaluate = lambda tab, expression, timeout=10: captured.append(expression) or next(results)
    result = browser.wait_for("owned-tab", role="button", name="Continue", timeout_ms=1000)
    assert result["ok"] and len(captured) == 2
    assert all("wantedName" in expression for expression in captured)


def test_browser_wait_for_timeout_is_explicit():
    browser = Browser.__new__(Browser)
    browser.evaluate = lambda *args, **kwargs: {"ok": False, "state": "visible", "matches": 0}
    result = browser.wait_for("owned-tab", selector="#late", timeout_ms=100)
    assert result["ok"] is False and result["timed_out"] is True


@pytest.mark.parametrize("key", ["Enter", "Tab", "Escape", "ArrowDown", "Ctrl+A", "Shift+Tab"])
def test_browser_key_allowlist_accepts_common_keys(key):
    assert parse_key(key)["code"]


@pytest.mark.parametrize("key", ["Alt+F4", "Ctrl+W", "Meta+Q", "launch-app", ""])
def test_browser_key_allowlist_rejects_window_or_unknown_shortcuts(key):
    with pytest.raises(PolicyError):
        parse_key(key)


def test_browser_press_focuses_owned_element_then_dispatches_cdp_key():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}
    browser.evaluate = lambda *args, **kwargs: {"ok": True, "focused": True}
    calls = []
    browser.call = lambda method, params=None, session=None, timeout=10: calls.append((method, params, session)) or {}
    result = browser.press("owned-tab", "Enter", role="button", name="Continue")
    assert result["ok"]
    assert [item[0] for item in calls] == ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"]
    assert calls[0][1]["type"] == "keyDown" and calls[1][1]["type"] == "keyUp"
    assert all(item[2] == "owned-session" for item in calls)


def test_browser_select_option_requires_native_select_and_verifies_selection():
    browser = Browser.__new__(Browser)
    captured = {}
    browser.evaluate = lambda tab, expression, timeout=10: captured.update(expression=expression) or {"ok": True, "value": "VN", "label": "Vietnam"}
    result = browser.element("owned-tab", "select_option", selector="#country", value="VN")
    assert result["ok"] and result["value"] == "VN"
    assert "e.tagName!=='SELECT'" in captured["expression"]
    assert "option_not_found" in captured["expression"]


def test_browser_read_redacts_token_like_fields():
    browser = Browser.__new__(Browser)
    captured = {}
    browser.evaluate = lambda tab, expression, timeout=10: captured.update(expression=expression) or {"elements": []}
    browser.read("owned-tab")
    assert "password|secret|token|otp|one-time-code" in captured["expression"]
    assert "value:secret?'[redacted]'" in captured["expression"]


def test_host_dispatch_routes_new_browser_actions(engine):
    class FakeBrowser:
        deadline = None
        tabs = {"owned-tab": "session"}
        def snapshot(self, tab): return {"ok": True, "tab": tab}
        def wait_for(self, *args): return {"ok": True, "args": args}
        def press(self, *args): return {"ok": True, "args": args}
        def history(self, tab, action): return {"ok": True, "tab": tab, "action": action}
        def element(self, *args): return {"ok": True, "args": args}
    fake = FakeBrowser()
    sid = "c" * 32
    engine.sessions[sid] = {"kind": "browser", "expires": time.time() + 60, "object": fake}
    assert engine.dispatch("browser", {"session_id": sid, "action": "snapshot", "tab_id": "owned-tab"}) == {"ok": True, "tab": "owned-tab"}
    assert engine.dispatch("browser", {"session_id": sid, "action": "reload", "tab_id": "owned-tab"})["action"] == "reload"
