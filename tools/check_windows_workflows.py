"""Native Windows smoke checks. Never grants access or types/clicks on a user's desktop.

Optional --visible-browser opens only the temporary UDB-owned browser window.
Optional --notify requests one UDB toast; its visual display requires human review.
Clipboard is read for format/API validation only and never printed or modified.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visible-browser", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Run these acceptance checks in an interactive Windows session")

    from udb.core import Agent
    from udb.desktop_control import InputControl, PauseHotkey, pause_local
    from udb.host_browser import Browser
    from udb.windows_utilities import WindowsBackend

    report = {}
    with tempfile.TemporaryDirectory(prefix="udb-windows-workflows-") as folder:
        agent = Agent(Path(folder) / "home")
        control = InputControl(agent)
        report["input_observation"] = control.status()["state"]
        pause_local(agent, True)
        assert control.status()["state"] == "paused"
        pause_local(agent, False)
        report["local_pause"] = "PASS"
        hotkey = PauseHotkey(agent)
        try:
            report["emergency_hotkey_registered"] = hotkey.available
        finally:
            hotkey.close()
        backend = WindowsBackend()
        report["start_menu_apps"] = len(backend.apps())
        clipboard = backend.clipboard()
        assert "version" in clipboard
        report["clipboard_format"] = clipboard["format"]
        # Do not retain or print any clipboard contents.
        del clipboard
        if args.notify:
            report["notification"] = backend.notify(
                "UDB", "Windows workflow acceptance check"
            )
        browser = Browser(Path(folder) / "browser")
        try:
            tab = next(iter(browser.tabs))
            assert len(browser.screenshot(tab)) > 100
            report["owned_browser_screenshot"] = "PASS"
            if args.visible_browser:
                old_tabs = set(browser.tabs)
                result = browser.switch_presentation(True)
                assert result["visible"] and not old_tabs.intersection(browser.tabs)
                browser.switch_presentation(False)
                assert not browser.visible
                report["browser_handoff"] = (
                    "PASS; verify visible window and AI label manually"
                )
        finally:
            browser.close()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
