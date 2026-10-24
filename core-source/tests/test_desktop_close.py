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
