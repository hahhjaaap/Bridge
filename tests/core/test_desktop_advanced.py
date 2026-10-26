"""Preflight, partial workflows and takeover cleanup without real user input."""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from udb.computer_desktop import Desktop
from udb.core import PolicyError


def test_nested_batch_is_rejected_before_any_action():
    desktop = Desktop.__new__(Desktop)
    calls = []

    def dispatch(action, window_id="", text=""):
        calls.append(action)

    desktop._dispatch = dispatch
    with pytest.raises(PolicyError):
        desktop.batch(
            "owned", [{"action": "type", "text": "safe"}, {"action": "batch"}]
        )
    assert calls == []


def test_batch_stops_after_failure_without_replay_or_rollback():
    desktop = Desktop.__new__(Desktop)
    calls = []

    def dispatch(action, window_id="", text=""):
        calls.append(action)
        if action == "invoke":
            raise PolicyError("fixture refuses invoke")
        return {"accepted": True}

    desktop._dispatch = dispatch
    row = desktop.batch(
        "owned",
        [
            {"action": "type", "text": "once"},
            {"action": "invoke"},
            {"action": "type", "text": "never"},
        ],
    )
    assert not row["ok"] and row["completed_steps"] == 1 and row["failed_index"] == 1
    assert calls == ["type", "invoke"] and row["rollback"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"button": "invalid"},
        {"clicks": True},
        {"clicks": 3},
        {"duration_ms": 1501},
        {"horizontal": 1},
        {"wait_ms": 10001},
        {"condition": "regex"},
    ],
)
def test_invalid_input_options_refused(options):
    with pytest.raises(PolicyError):
        Desktop.validate_options("click", **options)


def test_region_capture_cannot_escape_active_display():
    desktop = Desktop.__new__(Desktop)
    desktop.display_inventory = lambda: {"displays": [{"bounds": [0, 0, 1920, 1080]}]}
    desktop.capture = lambda box: {"bounds": box}
    assert desktop.display_capture(-1, [10, 10, 100, 100])["bounds"] == [
        10,
        10,
        100,
        100,
    ]
    for box in (
        [-1, 0, 100, 100],
        [0, 0, 2000, 100],
        [100, 0, 10, 10],
        [0, 0, True, 10],
    ):
        with pytest.raises(PolicyError):
            desktop.display_capture(-1, box)


def test_drag_releases_injected_button_when_user_moves_pointer():
    desktop = Desktop.__new__(Desktop)
    desktop.checked_point = lambda *a: None
    desktop.interactive = lambda: None
    desktop.gui = SimpleNamespace(GetForegroundWindow=lambda: 10)
    flags = []
    points = []
    reads = iter([(0, 0), (100, 100)])
    api = SimpleNamespace(
        SetCursorPos=lambda p: points.append(p), GetCursorPos=lambda: next(reads)
    )
    desktop.send = lambda hwnd, events: flags.extend(e.data.mi.dwFlags for e in events)

    def send_input(n, events, size):
        flags.extend(events[i].data.mi.dwFlags for i in range(n))
        return n

    desktop.u = SimpleNamespace(SendInput=send_input)
    with (
        patch.dict(sys.modules, {"win32api": api}),
        patch("udb.computer_desktop.time.sleep", lambda _: None),
    ):
        with pytest.raises(PolicyError, match="User moved"):
            desktop.mouse_action(10, "drag", 10, 10, 0, 0, 100, "left")
    assert flags == [2, 4]
    assert points == [(0, 0), (2, 2)]
