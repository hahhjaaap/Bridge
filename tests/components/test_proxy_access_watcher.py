"""Regression tests for transient failures in the proxy access watcher."""

from unittest.mock import Mock

import pytest

from udb.proxy import UnifiedProxy


def test_access_watcher_survives_companion_disconnect():
    primary = Mock()
    companion = Mock()
    primary.request.side_effect = RuntimeError("synthetic primary disconnect")
    companion.request.side_effect = RuntimeError("synthetic companion disconnect")
    proxy = UnifiedProxy(primary, companion)
    # Two monitor iterations, then exit the test without a real timer/thread.
    proxy.closed = Mock()
    proxy.closed.wait.side_effect = [False, False, True]

    proxy._watch_access()

    assert primary.request.call_count == 2
    assert companion.request.call_count == 2
    assert companion.request.call_args_list[0].args == ("udb/close-sessions",)


@pytest.mark.parametrize(
    "bad_resources", [[], {"at": "invalid", "admission_ready": True}]
)
def test_corrupt_host_status_never_reaches_companion(bad_resources):
    primary = Mock()
    companion = Mock()

    def upstream(method, params=None, **kwargs):
        if params["name"] == "computer_status":
            return {"result": {"structuredContent": {"enabled": True}}}
        return {"result": {"structuredContent": {"resources": bad_resources}}}

    primary.request.side_effect = upstream
    proxy = UnifiedProxy(primary, companion)
    proxy.tool_routes = {"terminal_start": companion}
    with pytest.raises(RuntimeError, match="resource monitor unavailable"):
        proxy.call_tool({"name": "terminal_start", "arguments": {}})
    companion.request.assert_not_called()
