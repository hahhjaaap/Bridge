"""CDP transport regressions: no command replay, bounded events and fail-closed IO."""

import json
from collections import deque

import pytest

from udb.browser_cdp import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_EVENT_MARKERS,
    MAX_REPLY_BYTES,
    CDPChannel,
)
from udb.core import PolicyError
from udb.host_browser import Browser


class Process:
    def alive(self):
        return True


class Socket:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.sent = []
        self.timeouts = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self):
        return self.messages.popleft()


def test_cdp_channel_retains_only_bounded_event_names():
    messages = [
        json.dumps(
            {
                "method": "Target.targetCreated",
                "params": {"targetInfo": {"url": "https://example.com/?token=secret"}},
            }
        ),
        json.dumps(
            {
                "method": "Network.requestWillBeSent",
                "params": {"headers": {"Cookie": "secret"}},
            }
        ),
        json.dumps({"id": 1, "result": {"ok": True}}),
    ]
    socket = Socket(messages)
    channel = CDPChannel(socket, Process())
    assert channel.call("Browser.getVersion", timeout=1) == {"ok": True}
    assert socket.sent == [{"id": 1, "method": "Browser.getVersion", "params": {}}]
    assert channel.event_markers() == (
        "Target.targetCreated",
        "Network.requestWillBeSent",
    )
    assert "secret" not in repr(channel.event_markers())


def test_cdp_channel_discards_late_reply_without_reusing_old_result():
    socket = Socket(
        [
            json.dumps({"id": 0, "result": {"unsafe": True}}),
            json.dumps({"id": 1, "result": {"value": "expected"}}),
            json.dumps({"id": 2, "result": {"value": "second"}}),
        ]
    )
    channel = CDPChannel(socket, Process())
    assert channel.call("Page.getFrameTree") == {"value": "expected"}
    assert channel.call("Page.getFrameTree") == {"value": "second"}
    assert [item["id"] for item in socket.sent] == [1, 2]


@pytest.mark.parametrize("payload", ["not-json", "[]", "null", "false"])
def test_cdp_channel_rejects_invalid_json_envelopes(payload):
    channel = CDPChannel(Socket([payload]), Process())
    with pytest.raises(PolicyError, match="Invalid browser protocol reply"):
        channel.call("Page.getFrameTree", timeout=1)


def test_cdp_channel_rejects_oversized_reply():
    channel = CDPChannel(Socket(["x" * (MAX_REPLY_BYTES + 1)]), Process())
    with pytest.raises(PolicyError, match="exceeds limit"):
        channel.call("Page.getFrameTree", timeout=1)


def test_cdp_channel_bounded_event_storage():
    messages = [
        json.dumps({"method": "Page.loadEventFired", "params": {"url": "private"}})
        for _ in range(MAX_EVENT_MARKERS + 3)
    ] + [json.dumps({"id": 1, "result": {}})]
    channel = CDPChannel(Socket(messages), Process())
    assert channel.call("Page.getFrameTree") == {}
    assert len(channel.event_markers()) == MAX_EVENT_MARKERS


def test_cdp_channel_does_not_hide_protocol_errors():
    channel = CDPChannel(
        Socket([json.dumps({"id": 1, "error": {"message": "blocked"}})]),
        Process(),
    )
    with pytest.raises(PolicyError, match="Browser operation failed: blocked"):
        channel.call("Page.navigate")



def test_cdp_channel_enforces_timeout_type_and_upper_bound():
    channel = CDPChannel(Socket([]), Process())
    for timeout in (True, float("nan"), float("inf"), 0, -1, MAX_COMMAND_TIMEOUT_SECONDS + 1, "1"):
        with pytest.raises(PolicyError, match="Invalid browser command timeout"):
            channel.call("Page.getFrameTree", timeout=timeout)
    assert channel.seq == 0


def test_cdp_channel_applies_reply_limit_to_utf8_bytes_not_character_count():
    payload = "é" * (MAX_REPLY_BYTES // 2 + 1)
    assert len(payload) < MAX_REPLY_BYTES
    channel = CDPChannel(Socket([payload]), Process())
    with pytest.raises(PolicyError, match="exceeds limit"):
        channel.call("Page.getFrameTree", timeout=1)


def test_cdp_channel_does_not_treat_boolean_id_as_integer_response_id():
    socket = Socket(
        [
            json.dumps({"id": True, "result": {"wrong": True}}),
            json.dumps({"id": 1, "result": {"right": True}}),
        ]
    )
    channel = CDPChannel(socket, Process())
    assert channel.call("Page.getFrameTree") == {"right": True}

def test_browser_call_keeps_legacy_method_contract_and_deadline():
    browser = Browser.__new__(Browser)
    browser.proc = Process()
    browser.deadline = None

    class Channel:
        def call(self, *args):
            return args

    browser._cdp = Channel()
    assert browser.call("Page.getFrameTree", {"a": 1}, "session", 3) == (
        "Page.getFrameTree",
        {"a": 1},
        "session",
        3,
    )
