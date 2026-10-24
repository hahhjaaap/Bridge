"""Per-user, windowless manager. Existing grants remain unchanged.

No QApplication, tray icon, user window, or input hook is created here.
Stop requests are local files bound to this exact process lifetime, not MCP tools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from . import __version__
from .core import PolicyError, reject_links
from .desktop import DesktopController
from .host_client import atomic_json

STATUS_NAME = "headless-status.json"
STOP_NAME = "headless-stop.json"


def manager_status(state: Path) -> dict:
    fallback = dict(mode="unknown_or_stopped", gui_loaded=None, unrestricted_host_access=False)
    try:
        path = Path(state) / STATUS_NAME
        reject_links(path)
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return fallback
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("mode") != "headless" or
                data.get("phase") != "running" or not -2 <= time.time() - data["at"] <= 6):
            return fallback
        return dict(mode="headless", gui_loaded=data.get("gui_loaded") is True,
                    unrestricted_host_access=False)
    except (OSError, ValueError, KeyError, TypeError, PolicyError):
        return fallback


def stop_requested(path: Path, instance: str, now=None) -> bool:
    try:
        reject_links(path)
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return False
        request = json.loads(raw)
        if not isinstance(request, dict):
            return False
        stamp = time.time() if now is None else now
        expiry = request.get("expires")
        return (request.get("instance") == instance and
                isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and
                stamp < expiry <= stamp + 60)
    except (OSError, ValueError, PolicyError):
        return False


def publish(controller, instance: str, phase="running"):
    connection = controller.status()
    atomic_json(controller.agent.state / STATUS_NAME, dict(
        mode="headless", version=__version__, pid=os.getpid(), instance=instance,
        at=time.time(), phase=phase,
        gui_loaded="PySide6.QtWidgets" in sys.modules,
        tunnel_ready=connection["ready"], tunnel_running=connection["tunnel_running"],
        connecting=connection["connecting"],
        permissions="existing_owner_grants", unrestricted_host_access=False))


def run_loop(controller, instance, stop_event, connect=True, interval=0.3):
    """Separate lifecycle so failures and cancellation can be tested without Qt."""
    try:
        controller.start_host()
        if connect:
            # Launch/retry asynchronously; local stop stays responsive during init.
            def begin():
                try:
                    controller.start_tunnel()
                except PolicyError:
                    pass  # Existing controller schedules bounded retries.
            threading.Thread(target=begin, daemon=True, name="UDB initial connection").start()
        while not stop_event.is_set():
            publish(controller, instance)
            if stop_requested(controller.agent.state / STOP_NAME, instance):
                break
            stop_event.wait(interval)
    finally:
        controller.close()
        publish(controller, instance, "stopped")


def run(home: Path, connect=True):
    if os.name != "nt":
        raise PolicyError("The headless Windows manager requires Windows.")
    # QtCore supplies the SAME OS-backed lease as the optional legacy GUI.
    # Importing QtCore creates no application object or window.
    from PySide6.QtCore import QLockFile
    home = Path(home)
    reject_links(home)
    if not home.is_dir():
        raise PolicyError("Select an existing UDB installation directory.")
    state = home / "state"
    reject_links(state)
    state.mkdir(exist_ok=True)
    lock = QLockFile(str(state / "manager.lock"))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(50):
        return 0  # Reuse the existing manager; never display a dialog.
    controller = None
    try:
        controller = DesktopController(home)
        instance = uuid.uuid4().hex
        stop = threading.Event()
        def signal_stop(*_):
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal_stop)
        run_loop(controller, instance, stop, connect=connect)
    finally:
        if controller is not None and not controller.closed.is_set():
            controller.close()
        lock.unlock()
    return 0
