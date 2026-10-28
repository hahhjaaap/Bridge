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
    if os.name == "nt" or not shutil.which("chromium"):
        pytest.skip("Linux Chromium fixture")

    class Process:
        def __init__(self, argv, cwd, memory_mib=0):
            self.process = subprocess.Popen(
                # The adapter exercises CDP/profile handoff without a display
                # server. Native Windows window visibility is a separate check.
                argv
                if "--headless=new" in argv
                else [*argv[:-1], "--headless=new", argv[-1]],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        def alive(self):
            return self.process.poll() is None

        def close(self):
            self.process.terminate()
            self.process.wait(timeout=10)

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

    monkeypatch.setattr(
        "udb.host_browser.find_browser", lambda: shutil.which("chromium")
    )
    monkeypatch.setattr("udb.host_browser.OwnedProcess", Process)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
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
            if pattern != "*://127.*/*"
        ),
    )
    browser = None
    try:
        browser = Browser(tmp_path / "profile")
        tab = next(iter(browser.tabs))
        browser.navigate(tab, fixture_url)
        yield browser, tab, fixture_url, tmp_path
    finally:
        if browser:
            browser.close()
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
