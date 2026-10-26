"""Regression tests for the desktop manager refresh extraction."""


import pytest


def test_main_window_uses_dedicated_refresh_mixin():
    pytest.importorskip("PySide6.QtCore")
    # Import Qt UI lazily so headless tests can inspect the module state first.
    from udb.manager import MainWindow
    from udb.manager_refresh import ManagerRefreshMixin

    assert issubclass(MainWindow, ManagerRefreshMixin)
    assert MainWindow.refresh is ManagerRefreshMixin.refresh
