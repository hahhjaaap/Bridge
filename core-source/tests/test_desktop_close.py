"""Controller shutdown is single-shot even if Qt closes twice."""
from __future__ import annotations

import threading
from types import SimpleNamespace


def test_controller_close_is_idempotent(monkeypatch):
    from udb.desktop import DesktopController
    import udb.desktop_vm as desktop_vm

    calls = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append("close:" + self.name)

    controller = object.__new__(DesktopController)
    controller.closed = threading.Event()
    controller.lock = threading.RLock()
    controller.agent = SimpleNamespace()
    controller.host_service = Resource("host")
    controller.children = {"mcp": object()}
    controller.handles = {"h1": Resource("h1")}

    def stop_process(name):
        calls.append("stop:" + name)
        controller.children.pop(name, None)

    controller.stop_process = stop_process
    monkeypatch.setattr(desktop_vm, "close_owned", lambda agent: calls.append("close:owned"))

    controller.close()
    controller.close()
    assert controller.closed.is_set()
    assert calls == ["close:host", "close:owned", "stop:mcp", "close:h1"]


def test_controller_close_does_not_shutdown_unowned_resources(monkeypatch):
    from udb.desktop import DesktopController
    import udb.desktop_vm as desktop_vm

    calls = []
    controller = object.__new__(DesktopController)
    controller.closed = threading.Event()
    controller.lock = threading.RLock()
    controller.agent = SimpleNamespace()
    controller.host_service = None
    controller.children = {}
    controller.handles = {}
    monkeypatch.setattr(desktop_vm, "close_owned", lambda agent: calls.append("owned"))
    controller.close()
    controller.close()
    assert calls == ["owned"]


def test_controller_close_attempts_all_owned_cleanup_after_errors(monkeypatch):
    from udb.desktop import DesktopController
    import udb.desktop_vm as desktop_vm

    calls = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("synthetic failure")

    controller = object.__new__(DesktopController)
    controller.closed = threading.Event()
    controller.lock = threading.RLock()
    controller.agent = SimpleNamespace()
    controller.host_service = Resource("host", fail=True)
    controller.children = {"mcp": object()}
    controller.handles = {"log": Resource("log")}

    def stop_process(name):
        calls.append("stop:" + name)
        raise RuntimeError("synthetic stop failure")

    def fail_owned(agent):
        calls.append("owned")
        raise RuntimeError("synthetic owned failure")

    controller.stop_process = stop_process
    monkeypatch.setattr(desktop_vm, "close_owned", fail_owned)

    controller.close()
    controller.close()
    assert calls == ["host", "owned", "stop:mcp", "log"]
    assert controller.error == "Cleanup completed with errors: RuntimeError, RuntimeError, RuntimeError"
