"""Frozen headless lifecycle on Windows with disposable state and no network/key access."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def check(exe):
    if os.name != "nt":
        raise RuntimeError("Windows required")
    import win32gui, win32process
    spec = importlib.util.spec_from_file_location("udb_cli", Path(__file__).resolve().parents[1]/"command"/"udb_cli.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    exe = Path(exe).resolve()
    with tempfile.TemporaryDirectory(prefix="udb-headless-accept-") as temp:
        home = Path(temp)
        (home/"state").mkdir()
        prefs = home/"state"/"host-settings.json"
        prefs.write_text('{"enabled":false}')
        log = home/"daemon.log"
        with log.open("wb") as output:
            # Default executable mode must be headless, even without --headless.
            process = subprocess.Popen([str(exe), "--no-connect", "--home", str(home)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until = time.monotonic()+25
            state = None
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise RuntimeError("Headless candidate exited: "+log.read_text(errors="replace")[-4000:])
                state = cli.headless_state(home)
                if state and cli.manager_running(home, exe):
                    break
                time.sleep(.1)
            assert state and state["mode"] == "headless" and not state["gui_loaded"]
            assert state["unrestricted_host_access"] is False
            windows = []
            def collect(hwnd, _):
                if win32process.GetWindowThreadProcessId(hwnd)[1] == state["pid"] and win32gui.IsWindowVisible(hwnd):
                    windows.append(hwnd)
            win32gui.EnumWindows(collect, None)
            assert not windows, "Headless manager created a visible window"
            duplicate = subprocess.run([str(exe), "--headless", "--no-connect", "--home", str(home)],
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            assert duplicate.returncode == 0
            assert cli.headless_state(home)["instance"] == state["instance"]
            assert prefs.read_text() == '{"enabled":false}'
            # A stale/foreign stop file must not kill the live agent.
            stop_path = home/"state"/"headless-stop.json"
            stop_path.write_text(json.dumps(dict(instance="f"*32, expires=time.time()+20)))
            time.sleep(.5)
            assert process.poll() is None
            result = cli.stop(home, exe, wait=15)
            assert not result["manager_running"]
            assert process.wait(timeout=10) == 0
            assert json.loads((home/"state"/"headless-status.json").read_text())["phase"] == "stopped"
            print(json.dumps(dict(headless_frozen="PASS", default_headless=True,
                no_visible_windows=True, qtwidgets_not_loaded=True, duplicate_reused=True,
                foreign_stop_ignored=True, graceful_stop=True, owner_preferences_unchanged=True,
                unrestricted_host_access=False, network_used=False)))
        finally:
            if process.poll() is None:
                state = cli.headless_state(home)
                if state:
                    (home/"state"/"headless-stop.json").write_text(json.dumps(
                        dict(instance=state["instance"], expires=time.time()+30)))
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate(); process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--exe", required=True)
    check(parser.parse_args().exe)
