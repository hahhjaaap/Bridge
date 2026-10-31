"""Regression tests for transient failures in the proxy access watcher."""

from unittest.mock import Mock

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
