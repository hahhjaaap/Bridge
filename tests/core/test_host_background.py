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
        "http://localhost./",
        "https://metadata.google.internal/",
        "https://example.com:bad/",
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
    browser.tabs = {"owned-tab": "owned-session"}
    captured = {}
    browser.evaluate = lambda tab, expression, timeout=10: captured.update(
        tab=tab, expression=expression, timeout=timeout
    ) or {"ok": True, "x": 12, "y": 34}
    calls = []
    browser.call = lambda method, params=None, session=None, timeout=10: (
        calls.append((method, params, session)) or {}
    )
    result = browser.element("owned-tab", "click", role="button", name="Save")
    assert result["ok"] and result["clicked"]
    assert captured["tab"] == "owned-tab"
    assert '"button"' in captured["expression"]
    assert '"Save"' in captured["expression"]
    assert "locator_not_unique" in captured["expression"]
    assert "scrollIntoView" in captured["expression"]
    assert "elementFromPoint" in captured["expression"]
    assert [call[1]["type"] for call in calls] == [
        "mouseMoved", "mousePressed", "mouseReleased"
    ]
    assert all(call[2] == "owned-session" for call in calls)


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
    calls.clear()
    result = browser.press("owned-tab", "a", role="textbox", name="Search")
    assert result["ok"]
    assert calls[0][1]["text"] == "a"
    assert "text" not in calls[1][1] and "unmodifiedText" not in calls[1][1]


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

def test_browser_attach_installs_network_blocklist_for_each_owned_tab():
    browser = Browser.__new__(Browser)
    browser.tabs = {}
    calls = []

    def fake_call(method, params=None, session=None, timeout=10):
        calls.append((method, params, session))
        if method == "Target.attachToTarget":
            return {"sessionId": "owned-session"}
        return {}

    browser.call = fake_call
    assert browser.attach("owned-tab") == "owned-tab"
    methods = [call[0] for call in calls]
    assert "Network.enable" in methods
    blocked = next(call for call in calls if call[0] == "Network.setBlockedURLs")
    assert "*://127.*/*" in blocked[1]["urls"]
    assert "*://localhost/*" in blocked[1]["urls"]
    assert blocked[2] == "owned-session"


def test_browser_listing_adopts_owned_popups_and_closes_excess_tabs():
    browser = Browser.__new__(Browser)
    browser.tabs = {"one": "session-one", "two": "session-two", "three": "session-three"}
    targets = [
        {
            "type": "page",
            "targetId": tab,
            "title": tab,
            "url": "chrome://settings" if tab == "internal" else "https://example.com/?token=secret",
        }
        for tab in ("one", "two", "three", "internal", "popup", "excess")
    ]
    closed = []
    browser.call = lambda method, params=None, session=None, timeout=10: (
        {"targetInfos": targets}
        if method == "Target.getTargets"
        else closed.append(params["targetId"]) or {"success": True}
    )

    def attach(tab):
        browser.tabs[tab] = "session-" + tab
        return tab

    browser.attach = attach
    listing = browser.listing()
    assert [row["tab_id"] for row in listing] == ["one", "two", "three", "popup"]
    assert browser.tabs["popup"] == "session-popup"
    assert all(row["url"] == "https://example.com/" for row in listing)
    assert "internal" not in browser.tabs
    assert closed == ["internal", "excess"]


def test_browser_navigation_waits_for_complete_document_and_redacts_query():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}
    states = iter([
        {"readyState": "loading", "href": "https://example.com/path?secret=1"},
        {"readyState": "complete", "href": "https://example.com/path?secret=1"},
    ])
    browser.evaluate = lambda tab, expression, timeout=10: (
        "https://old.example/" if expression == "location.href" else next(states)
    )
    calls = []
    browser.call = lambda method, params=None, session=None, timeout=10: (
        calls.append((method, params, session)) or {"loaderId": "new-loader"}
        if method == "Page.navigate"
        else {}
    )
    result = browser.navigate("owned-tab", "https://example.com/path?secret=1")
    assert result["navigation_completed"] and result["ready_state"] == "complete"
    assert result["url"] == "https://example.com/path"
    assert result["tab_id"] == "owned-tab"
    assert calls[0][0] == "Page.navigate"


def test_browser_navigation_times_out_if_document_stays_loading():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}
    browser.evaluate = lambda tab, expression, timeout=10: (
        "https://old.example/" if expression == "location.href"
        else {"readyState": "loading", "href": "https://example.com/"}
    )
    browser.call = lambda method, params=None, session=None, timeout=10: (
        {"loaderId": "new-loader"} if method == "Page.navigate" else {}
    )
    with pytest.raises(PolicyError, match="Navigation timed out"):
        browser.navigate("owned-tab", "https://example.com/", timeout_ms=100)


def test_browser_click_waits_for_actionability_then_dispatches_pointer_events():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}
    results = iter([
        {"ok": False, "error": "locator_not_unique", "matches": 0},
        {"ok": False, "error": "not_actionable", "reason": "obscured"},
        {"ok": True, "x": 50, "y": 80},
    ])
    expressions = []
    browser.evaluate = lambda tab, expression, timeout=10: (
        "https://example.com/" if expression == "location.href"
        else expressions.append(expression) or next(results)
    )
    calls = []
    browser.call = lambda method, params=None, session=None, timeout=10: (
        calls.append((method, params, session)) or {}
    )
    browser._wait_for_click_navigation = lambda *args, **kwargs: {
        "navigation_completed": False
    }
    result = browser.element("owned-tab", "click", selector="#continue")
    assert result["ok"]
    assert len(expressions) == 3
    assert [call[1]["type"] for call in calls] == [
        "mouseMoved", "mousePressed", "mouseReleased"
    ]


def test_browser_ambiguous_click_does_not_dispatch_mouse_events():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}
    browser.evaluate = lambda *args, **kwargs: {
        "ok": False, "error": "locator_not_unique", "matches": 2
    }
    browser.call = lambda *args, **kwargs: pytest.fail("ambiguous locator must not click")
    result = browser.element("owned-tab", "click", selector=".save")
    assert not result["ok"] and result["matches"] == 2

def test_browser_click_waits_for_navigation_and_redacts_query_string():
    browser = Browser.__new__(Browser)
    browser.tabs = {"owned-tab": "owned-session"}

    def evaluate(tab, expression, timeout=10):
        if expression == "location.href":
            return "https://old.example/"
        if "readyState" in expression:
            return {
                "readyState": "complete",
                "href": "https://new.example/result?session=secret",
            }
        return {"ok": True, "x": 21, "y": 42}

    browser.evaluate = evaluate
    browser.call = lambda *args, **kwargs: {}
    result = browser.element("owned-tab", "click", selector="#continue")
    assert result["ok"] and result["clicked"]
    assert result["navigation_completed"]
    assert result["url"] == "https://new.example/result"