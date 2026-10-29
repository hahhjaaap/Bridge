"""Windows action routing and policy gates without interacting with user windows."""

import json
from types import SimpleNamespace

import pytest

from udb.computer_desktop import Desktop
from udb.core import PolicyError


def test_mcp_rejects_non_boolean_checkbox_state_before_access_or_dispatch(
    tmp_path, monkeypatch
):
    import asyncio

    from udb.core import Agent
    from udb.server import make_server

    monkeypatch.setattr(
        "udb.computer_access.require",
        lambda agent: pytest.fail("validation must precede dispatch"),
    )
    server = make_server(Agent(tmp_path))
    with pytest.raises(Exception, match="valid boolean"):
        asyncio.run(
            server.call_tool(
                "computer_desktop",
                {"action": "set_checked", "window_id": "observed", "checked": "false"},
            )
        )


def test_uia_request_uses_json_data_and_preserves_state_fields(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    desktop.interactive = lambda: None
    desktop.process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 22))
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"checked":true}')

    monkeypatch.setattr("udb.computer_desktop.subprocess.run", run)
    assert desktop.uia(
        42, "set_checked", automation_id="literal';not-code", checked=True, limit=12
    )["checked"]
    argv, options = calls[0]
    request = json.loads(options["input"])
    assert (
        request["pid"] == 22 and request["checked"] is True and request["limit"] == 12
    )
    assert request["automation_id"] == "literal';not-code"
    assert all("literal" not in arg for arg in argv) and options["shell"] is False


@pytest.mark.parametrize(
    "output", ["[]", '{"ok":false,"error":"PRIVATE_HELPER_DETAIL"}', "x" * 262145]
)
def test_uia_malformed_or_failed_response_is_not_replayed_or_echoed(
    monkeypatch, output
):
    desktop = Desktop.__new__(Desktop)
    desktop.interactive = lambda: None
    desktop.process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 22))
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("udb.computer_desktop.subprocess.run", run)
    with pytest.raises(PolicyError, match="inspect the target") as error:
        desktop.uia(42, "select", automation_id="item")
    assert len(calls) == 1 and "PRIVATE_HELPER_DETAIL" not in str(error.value)


def test_discovery_consolidates_windows_interfaces_without_understating_launch_effects():
    from udb.registry import catalog

    data = catalog()
    tools = {row["name"]: row for row in data["tools"]}
    assert data["tool_count"] == 59 and data["backend_count"] == 67
    assert "computer_execute" not in tools and "computer_utilities" not in tools
    assert tools["computer_process"]["annotations"]["destructiveHint"] is True
    assert tools["computer_process"]["annotations"]["openWorldHint"] is True
    assert {
        "set_checked",
        "select",
        "read_text",
        "close_window",
        "clipboard_get",
        "launch_app",
    } <= set(tools["computer_desktop"]["inputSchema"]["properties"]["action"]["enum"])


@pytest.mark.parametrize(
    "action",
    ["read_text", "set_checked", "select", "expand", "collapse", "scroll_into_view"],
)
def test_uia_actions_do_not_focus_or_fall_back_to_physical_input(action):
    desktop = Desktop.__new__(Desktop)
    checks, calls = [], []
    desktop.control = SimpleNamespace(check=lambda: checks.append(True))

    def resolve(window_id, focus=True):
        assert window_id == "observed" and focus is False
        return 42

    desktop.resolve = resolve
    desktop.uia = lambda *args: calls.append(args) or {"ok": True}
    result = desktop._dispatch(
        action, window_id="observed", automation_id="control", checked=True, limit=10
    )
    assert result["ok"] and len(calls) == 1 and calls[0][0:2] == (42, action)
    assert checks == ([] if action == "read_text" else [True])


@pytest.mark.parametrize("checked", [None, "false", 0, 1])
def test_checkbox_rejects_non_boolean_state_before_resolving_a_window(checked):
    desktop = Desktop.__new__(Desktop)
    desktop.resolve = lambda *args, **kwargs: pytest.fail("must reject first")
    with pytest.raises(PolicyError, match="checked=true or false"):
        desktop._dispatch("set_checked", checked=checked)


def test_failed_uia_action_is_not_replayed():
    desktop = Desktop.__new__(Desktop)
    desktop.resolve = lambda *args, **kwargs: 42
    calls = []

    def refused(*args):
        calls.append(args)
        raise PolicyError("Outcome unknown; inspect")

    desktop.uia = refused
    with pytest.raises(PolicyError, match="Outcome unknown"):
        desktop._dispatch("select", window_id="observed", automation_id="item")
    assert len(calls) == 1


@pytest.mark.parametrize("closed", [True, False])
def test_window_close_is_a_single_request_without_force_or_focus(closed):
    desktop = Desktop.__new__(Desktop)
    calls = []

    def resolve(window_id, focus=True):
        assert focus is False
        return 42

    desktop.resolve = resolve
    desktop.gui = SimpleNamespace(
        PostMessage=lambda *args: calls.append(args), IsWindow=lambda hwnd: not closed
    )
    result = desktop._dispatch("close_window", window_id="observed", wait_ms=0)
    assert calls == [(42, 0x0010, 0, 0)]
    assert result == dict(
        requested=True, closed_observed=closed, replay_safe=False, forced=False
    )
