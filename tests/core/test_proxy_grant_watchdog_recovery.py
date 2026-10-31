"""Regression: transient cleanup errors must not kill the UDB grant watchdog."""

from udb.proxy import UnifiedProxy


class StopAfter:
    def __init__(self, cycles):
        self.cycles = cycles
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > self.cycles


class DisabledPrimary:
    def __init__(self):
        self.calls = 0

    def request(self, method, params, timeout=5):
        assert method == "tools/call"
        assert params["name"] == "computer_status"
        self.calls += 1
        return {
            "result": {
                "structuredContent": {
                    "enabled": False,
                    "capabilities": [],
                }
            }
        }


class FlakyCompanion:
    def __init__(self, failures=2):
        self.failures = failures
        self.calls = []

    def request(self, method, timeout=10):
        assert method == "udb/close-sessions"
        self.calls.append(method)
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient synthetic cleanup failure")
        return {"result": {}}


def test_watcher_retries_after_both_cleanup_attempts_fail():
    primary = DisabledPrimary()
    companion = FlakyCompanion(failures=2)
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = StopAfter(2)

    proxy._watch_access()

    # Before the fix, the fallback's second failure returned from _watch_access
    # permanently, so there was never a second grant check.
    assert primary.calls == 2
    assert companion.calls == ["udb/close-sessions"] * 3
    assert proxy._pointer_presence_started is False


def test_watcher_stops_cleanly_only_when_closed():
    primary = DisabledPrimary()
    companion = FlakyCompanion(failures=20)
    proxy = UnifiedProxy(primary, companion)
    proxy.closed = StopAfter(3)

    proxy._watch_access()

    assert primary.calls == 3
    assert companion.calls == ["udb/close-sessions"] * 6
