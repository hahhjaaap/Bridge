"""Exercise real CDP/DOM input and identity using an ephemeral Linux Chromium.

Windows job containment remains covered separately; this adapter is test-only.
"""

import base64
import http.server
import os
import shutil
import subprocess
import threading

import pytest

from udb import host_browser
from udb.host_browser import Browser


@pytest.fixture
def live_browser(tmp_path, monkeypatch):
    browser_exe = shutil.which("google-chrome") or shutil.which("chromium")
    if os.name == "nt" or not browser_exe:
        pytest.skip("Linux Chrome/Chromium fixture")

    class Process:
        def __init__(self, argv, cwd, memory_mib=0):
            self.log = (tmp_path / "chromium.log").open("w+b")
            self.process = subprocess.Popen(
                # The adapter exercises CDP/profile handoff without a display
                # server. Native Windows window visibility is a separate check.
                argv
                if "--headless=new" in argv
                else [*argv[:-1], "--headless=new", argv[-1]],
                cwd=cwd,
                stdout=self.log,
                stderr=self.log,
            )

        def alive(self):
            return self.process.poll() is None

        def close(self):
            import sys

            failed_start = self.process.poll() not in (None, 0)
            self.process.terminate()
            self.process.wait(timeout=10)
            if failed_start:
                self.log.seek(0)
                sys.stderr.write(self.log.read(16384).decode("utf-8", errors="replace"))
            self.log.close()

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.end_headers()
                return
            if self.path == "/file":
                self.send_response(
                    200
                    if "test_session=fixture" in self.headers.get("Cookie", "")
                    else 403
                )
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(b"download fixture")
                return
            if self.path.startswith("/forms"):
                self.server.form_requests.append(self.path)
                body = b"""<title>Forms</title>
                <form action="/forms" method="get">
                <label>Text<input name="text"></label>
                <input id="disabled" disabled><input id="readonly" readonly value="locked">
                <fieldset disabled><input id="fieldset-disabled"></fieldset>
                <label>Country<select id="country" name="country">
                <option value="">Choose</option><option value="VN">Vietnam</option>
                <option value="US">United States</option>
                <optgroup label="Unavailable" disabled><option value="XX">Blocked</option></optgroup>
                </select></label>
                <script>window.changes=0;document.querySelector('#country').addEventListener('change',()=>window.changes++);</script>
                <button>Submit</button></form>"""
            elif self.path.startswith("/destination"):
                body = b"<title>Destination</title><h1>Arrived</h1>"
            else:
                body = b"<title>Ready</title><button onclick=\"document.title='Clicked'\">Run</button><input type='file' id='upload'>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header(
                "Set-Cookie", "test_session=fixture; HttpOnly; SameSite=Lax"
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setattr("udb.host_browser.find_browser", lambda: browser_exe)
    monkeypatch.setattr("udb.host_browser.OwnedProcess", Process)
    # This Linux adapter checks CDP/DOM behavior, not Windows launch latency.
    # Cold Chrome on shared CI can exceed 20 seconds; keep a bounded fixture
    # budget without changing the production Windows startup limits.
    monkeypatch.setattr(host_browser, "_STARTUP_TIMEOUT", 60)
    monkeypatch.setattr(host_browser, "_ENDPOINT_TIMEOUT", 45)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    server.form_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_url = f"http://127.0.0.1:{server.server_port}/"
    from udb import browser_files

    validate = host_browser.validate_url
    fixture_urls = {
        fixture_url,
        fixture_url.rstrip("/"),
        fixture_url + "file",
        fixture_url + "redirect",
        fixture_url + "forms",
        fixture_url + "forms?text=&country=",
        fixture_url + "destination",
        fixture_url.replace("127.0.0.1", "localhost") + "destination",
    }
    # This test exercises CDP/DOM identity, not public-site network policy.
    # Permit only our exact local fixture; all other validation stays intact.
    monkeypatch.setattr(
        host_browser,
        "validate_url",
        lambda url: url if url in fixture_urls else validate(url),
    )
    monkeypatch.setattr(browser_files, "validate_url", host_browser.validate_url)
    monkeypatch.setattr(
        host_browser,
        "_BLOCKED_URL_PATTERNS",
        tuple(
            pattern
            for pattern in host_browser._BLOCKED_URL_PATTERNS
            if pattern not in {"*://127.*/*", "*://localhost/*"}
        ),
    )
    browser = None
    try:
        browser = Browser(tmp_path / "profile")
        browser.fixture_requests = server.form_requests
        tab = next(iter(browser.tabs))
        browser.navigate(tab, fixture_url)
        yield browser, tab, fixture_url, tmp_path
    finally:
        if browser:
            browser.close()
        elif (tmp_path / "chromium.log").exists():
            import sys

            sys.stderr.write(
                (tmp_path / "chromium.log").read_text(errors="replace")[-16384:]
            )
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_browser_background_click_identity_and_reload(live_browser):
    browser, tab, fixture_url, tmp_path = live_browser
    assert browser.evaluate(
        tab, "!!document.getElementById('__udb_owned_browser_identity')"
    )
    result = browser.element(tab, "click", role="button", name="Run")
    assert result["ok"], (result, browser.read(tab))
    assert browser.evaluate(tab, "document.title") == "Clicked"
    assert browser.evaluate(
        tab, "document.querySelector('link[data-udb-owned-icon]') !== null"
    )
    # Identity does not obscure the actual button or enter accessible locators.
    assert (
        browser.evaluate(
            tab,
            "document.getElementById('__udb_owned_browser_identity').getAttribute('aria-hidden')",
        )
        == "true"
    )
    screenshot = browser.screenshot(tab)
    assert len(screenshot) > 100
    (tmp_path / "browser.png").write_bytes(base64.b64decode(screenshot))
    browser.history(tab, "reload")
    browser._wait_for_document_ready(tab)
    assert browser.evaluate(
        tab, "!!document.getElementById('__udb_owned_browser_identity')"
    )
    assert browser.evaluate(tab, "document.title") == "Ready"


def test_real_browser_cookie_download_and_redirect_rejection(live_browser):
    from udb.browser_files import download

    browser, tab, url, _ = live_browser
    data, mime = download(browser, tab, url + "file", url)
    assert data == b"download fixture" and mime == "application/octet-stream"
    with pytest.raises(host_browser.PolicyError, match="redirected"):
        download(browser, tab, url + "redirect", url)


def test_real_browser_upload_retains_staged_bytes_for_file_reader(live_browser):
    from pathlib import Path

    from udb.browser_files import upload

    browser, tab, url, _ = live_browser
    result = upload(
        browser, tab, "#upload", Path("invoice.txt"), b"upload fixture", url
    )
    read = browser.call(
        "Runtime.evaluate",
        {
            "expression": "document.querySelector('#upload').files[0].text()",
            "awaitPromise": True,
            "returnByValue": True,
        },
        browser.sid(tab),
    )
    assert read["result"]["value"] == "upload fixture"
    assert result["submitted"] is False


def test_real_browser_handoff_invalidates_tabs_and_keeps_session_cookie(live_browser):
    browser, old_tab, url, _ = live_browser
    browser.call(
        "Network.setCookie",
        {"name": "handoff_only", "value": "preserved", "url": url, "httpOnly": True},
        browser.sid(old_tab),
    )
    result = browser.switch_presentation(True)
    assert result["tab_ids_invalidated"] and browser.visible
    assert old_tab not in browser.tabs
    cookies = browser.call("Storage.getCookies")["cookies"]
    assert any(
        c["name"] == "handoff_only" and c["value"] == "preserved" for c in cookies
    )
    browser.switch_presentation(False)
    assert not browser.visible
    tab = next(t["tab_id"] for t in browser.listing() if t["url"] == url)
    assert browser.read(tab)["title"] == "Ready"


def test_real_browser_persistent_owned_profile_survives_graceful_close(live_browser):
    import time

    browser, tab, url, _ = live_browser
    browser.persistent = True
    browser.call(
        "Network.setCookie",
        {
            "name": "saved_login_fixture",
            "value": "fixture",
            "url": url,
            "expires": time.time() + 3600,
        },
        browser.sid(tab),
    )
    profile = browser.folder
    browser.close()
    assert profile.is_dir()
    restored = Browser(profile, persistent=True)
    try:
        cookies = restored.call("Storage.getCookies")["cookies"]
        assert any(c["name"] == "saved_login_fixture" for c in cookies)
    finally:
        restored.close()


def test_real_browser_native_select_fill_and_disabled_group(live_browser):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms")
    selected = browser.element(tab, "select_option", selector="#country", value="VN")
    assert selected["ok"] and selected["value"] == "VN"
    selected = browser.element(tab, "fill", selector="#country", value="United States")
    assert selected["ok"] and selected["value"] == "US"
    assert browser.evaluate(tab, "window.changes") == 2
    option_click = browser.element(tab, "click", selector="#country option[value='VN']")
    assert option_click["error"] == "use_select_option" and not option_click.get(
        "timed_out"
    )
    rejected = browser.element(tab, "select_option", selector="#country", value="XX")
    assert rejected["ok"] is False and rejected["error"] == "disabled"
    assert browser.evaluate(tab, "document.querySelector('#country').value") == "US"
    assert browser.evaluate(tab, "window.changes") == 2
    assert browser.element(tab, "select_option", selector="#country", value="")["ok"]


def test_real_browser_select_value_precedes_label_and_ambiguity_is_not_written(
    live_browser,
):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms")
    browser.evaluate(tab, "document.querySelector('option[value=US]').label='VN'")
    result = browser.element(tab, "select_option", selector="#country", value="VN")
    assert result["ok"] and result["value"] == "VN"
    browser.evaluate(
        tab,
        "for(const o of document.querySelectorAll('#country option'))o.label='Duplicate'",
    )
    result = browser.element(
        tab, "select_option", selector="#country", value="Duplicate"
    )
    assert result["error"] == "option_not_unique"
    assert browser.evaluate(tab, "window.changes") == 1


def test_real_browser_rejected_selection_does_not_repeat_change_events(live_browser):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms")
    browser.evaluate(
        tab,
        "document.querySelector('#country').addEventListener('change',e=>e.target.value='')",
    )
    result = browser.element(tab, "select_option", selector="#country", value="VN")
    assert result["error"] == "state_mismatch" and result["ok"] is False
    assert browser.evaluate(tab, "window.changes") == 1


def test_real_browser_disabled_fill_reports_state_without_timeout(live_browser):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms")
    for selector, reason in [
        ("#disabled", "disabled"),
        ("#fieldset-disabled", "disabled"),
        ("#readonly", "readonly"),
    ]:
        result = browser.element(tab, "fill", selector=selector, value="changed")
        assert result["error"] == reason and not result.get("timed_out"), result
    assert (
        browser.evaluate(tab, "document.querySelector('#readonly').value") == "locked"
    )


def test_real_browser_repeated_same_url_submit_is_observed(live_browser):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms?text=&country=")
    for _ in range(3):
        result = browser.element(tab, "click", role="button", name="Submit")
        assert result["clicked"] and result["navigation_completed"], result
        assert browser.read(tab)["title"] == "Forms"
    assert browser.fixture_requests.count("/forms?text=&country=") == 4


def test_real_browser_snapshot_exposes_options_and_effective_state(live_browser):
    browser, tab, url, _ = live_browser
    browser.navigate(tab, url + "forms")
    for report in (browser.snapshot(tab), browser.read(tab)):
        elements = {row["id"]: row for row in report["elements"]}
        assert elements["readonly"]["readonly"]
        assert elements["fieldset-disabled"]["disabled"]
        options = elements["country"]["options"]
        assert options[1]["value"] == "VN" and options[-1]["disabled"]


def test_real_browser_cross_origin_link_click(live_browser):
    import json

    browser, tab, url, _ = live_browser
    destination = url.replace("127.0.0.1", "localhost") + "destination"
    browser.evaluate(
        tab,
        "document.body.innerHTML='<a id=next>Continue</a>';document.querySelector('#next').href="
        + json.dumps(destination),
    )
    result = browser.element(tab, "click", role="link", name="Continue")
    assert result["clicked"] and result["navigation_completed"], result
    assert browser.read(tab)["title"] == "Destination"
