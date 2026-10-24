"""Linux-executable GUI lifecycle regression without importing Windows/Qt."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "udb" / "manager.py"


def _run_function():
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    assert len(found) == 1
    return found[0]


def _simulate(*, stage="ok", lock_held=True):
    function = _run_function()
    # Execution is limited to the extracted run() function with disposable
    # fakes. It never imports Qt, executes Windows code or grants UDB access.
    events = []

    class FakeApp:
        def setProperty(self, *unused):
            pass

        def exec(self):
            events.append("exec")
            if stage == "event-loop":
                raise RuntimeError("event-loop failed")
            return 11

    class FakeController:
        def __init__(self, home):
            self.agent = SimpleNamespace(state=home / "state")

        def settings(self):
            return {"motion": False, "autoconnect": False}

        def start_host(self):
            events.append("start_host")
            if stage == "start-host":
                raise RuntimeError("host start failed")

        def close(self):
            events.append("close")

    class FakeLock:
        def __init__(self, path):
            pass

        def setStaleLockTime(self, seconds):
            pass

        def tryLock(self, timeout):
            events.append("try_lock")
            return lock_held

        def unlock(self):
            events.append("unlock")

    class FakeWindow:
        def __init__(self, home, controller):
            if stage == "construct-window":
                raise RuntimeError("window construction failed")

        def show(self):
            events.append("show")

    namespace = {
        "os": SimpleNamespace(name="posix"),
        "Path": Path,
        "make_app": FakeApp,
        "DesktopController": FakeController,
        "QLockFile": FakeLock,
        "MainWindow": FakeWindow,
        "QMessageBox": SimpleNamespace(information=lambda *args: events.append("busy")),
    }
    # Preserve postponed annotation behavior without imports of PySide6.
    compiled = compile(
        ast.fix_missing_locations(ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0),
                  function],
            type_ignores=[],
        )),
        str(SOURCE),
        "exec",
    )
    exec(compiled, namespace)
    try:
        value = namespace["run"](Path("/tmp/synthetic-udb"))
    except RuntimeError:
        value = "error"
    return events, value


@pytest.mark.parametrize("stage", ["start-host", "construct-window", "event-loop"])
def test_gui_run_always_cleans_up_after_failure(stage):
    events, result = _simulate(stage=stage)
    assert result == "error"
    assert events.count("close") == 1
    assert events.count("unlock") == 1
    assert events.index("unlock") > events.index("close")


def test_gui_run_releases_resources_after_success():
    events, result = _simulate()
    assert result == 11
    assert events.count("close") == 1
    assert events.count("unlock") == 1


def test_second_instance_does_not_start_host():
    events, result = _simulate(lock_held=False)
    assert result is None
    assert "start_host" not in events
    assert "unlock" not in events
    assert events.count("busy") == 1


def test_gui_entrypoints_are_annotated():
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"label", "row", "panel", "empty_state", "icon", "make_app", "run", "self_test"}
    for function in module.body:
        if isinstance(function, ast.FunctionDef) and function.name in names:
            assert function.returns is not None, function.name
            for argument in [*function.args.posonlyargs, *function.args.args]:
                assert argument.annotation is not None, f"{function.name}.{argument.arg}"
            if function.args.vararg is not None:
                assert function.args.vararg.annotation is not None
            names.remove(function.name)
    assert not names
