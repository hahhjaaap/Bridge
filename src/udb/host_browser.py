"""A browser owned by UDB, with a fresh profile and no visible/focusable window.

Never attaches to the user's browser, discovers their tabs or injects OS input.
All CDP verbs stay inside the exact process/profile launched here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from .browser_cdp import CDPChannel
from .browser_dom import (
    CLICK_POINT_SCRIPT,
    DOM_TRAVERSAL_SCRIPT,
    LOCATOR_SCRIPT,
    SELECT_OPTION_SCRIPT,
)
from .browser_identity import identity_script
from .browser_input import BrowserInputMixin
from .browser_input import parse_key as parse_key
from .browser_observation import read as read_browser
from .browser_observation import snapshot as snapshot_browser
from .core import PolicyError, reject_links
from .owned_process import OwnedProcess as OwnedProcess
from .resources import LIMITS

_STARTUP_TIMEOUT = 20
_ENDPOINT_TIMEOUT = 15


def _browser_endpoint(endpoint, process, timeout):
    """Chrome can publish the file before releasing its Windows write lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process.alive():
            raise PolicyError("Private browser exited during startup")
        try:
            with endpoint.open("rb") as stream:
                raw = stream.read(4097)
        except (FileNotFoundError, PermissionError):
            time.sleep(0.05)
            continue
        try:
            if len(raw) > 4096:
                raise ValueError("oversized endpoint")
            lines = raw.decode("ascii").splitlines()
            if len(lines) < 2 or not lines[1]:
                time.sleep(0.05)
                continue
            port, path = lines[:2]
            if (
                not port.isdigit()
                or not path.startswith("/devtools/browser/")
                or not 1 <= int(port) <= 65535
            ):
                raise ValueError("invalid endpoint")
        except (ValueError, UnicodeError) as exc:
            raise PolicyError("Invalid private browser endpoint") from exc
        return port, path
    raise PolicyError("Private browser startup timed out")


def validate_url(value):
    if value == "about:blank":
        return value
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(c) < 32 for c in value)
    ):
        raise PolicyError("Invalid URL")
    try:
        u = urlsplit(value)
        host = (u.hostname or "").lower().rstrip(".")
        # Accessing .port validates malformed/out-of-range port values.
        _ = u.port
    except ValueError as exc:
        raise PolicyError("Invalid URL") from exc
    if u.scheme not in ("https", "http") or not host or u.username or u.password:
        raise PolicyError("Only HTTP(S) pages without embedded credentials")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PolicyError("Local browser endpoints are private")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PolicyError("Private IP navigation blocked")
    return value


_BROWSER_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "heading",
        "tab",
        "menuitem",
        "switch",
        "slider",
        "spinbutton",
        "treeitem",
    }
)
# Defense in depth for redirects and subresources. Direct navigation is also
# validated in Python before the browser is asked to navigate.
_BLOCKED_URL_PATTERNS = tuple(
    [
        "*://localhost/*",
        "*://*.localhost/*",
        "*://*.local/*",
        "*://*.internal/*",
        "*://127.*/*",
        "*://10.*/*",
        "*://192.168.*/*",
        "*://169.254.*/*",
        "*://[::1]/*",
        "*://[fc00:*/*",
        "*://[fd00:*/*",
        "*://[fe80:*/*",
        "*://metadata.google.internal/*",
    ]
    + [f"*://172.{octet}.*/*" for octet in range(16, 32)]
)


def validate_locator(selector="", role="", name="", exact=True):
    if not isinstance(selector, str) or len(selector) > 500:
        raise PolicyError("CSS selector must be a string of at most 500 characters")
    if not isinstance(role, str) or len(role) > 40:
        raise PolicyError("Invalid browser role")
    if not isinstance(name, str) or len(name) > 500:
        raise PolicyError("Accessible name must be at most 500 characters")
    if type(exact) is not bool:
        raise PolicyError("exact must be boolean")
    if selector:
        if role or name:
            raise PolicyError("Use either a CSS selector or a role/name locator")
        return
    if name and not role:
        raise PolicyError("A semantic name requires a role")
    if role and role not in _BROWSER_ROLES:
        raise PolicyError("Unsupported browser role")
    if not role:
        raise PolicyError("Provide a CSS selector or an accessible role")


def _browser_launch_args(exe, folder, visible=False):
    """Build a minimal, compatibility-first command for an owned profile."""
    if type(visible) is not bool:
        raise PolicyError("Invalid browser presentation")
    args = [str(exe)]
    if not visible:
        args.append("--headless=new")
    args.extend(
        [
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=" + str(folder),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-default-apps",
            "--disk-cache-size=67108864",
            "--mute-audio",
            "about:blank",
        ]
    )
    return args


def find_browser():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    for p in candidates:
        if p.is_file():
            reject_links(p)
            return p
    raise PolicyError("Install Chrome or Edge before opening a background browser")


class Browser(BrowserInputMixin):
    def __init__(self, folder, *, persistent=False, visible=False, reuse_profile=False):
        self.persistent = persistent
        self.visible = visible
        self.folder = Path(folder)
        reject_links(self.folder)
        self.folder.mkdir(parents=True, exist_ok=persistent or reuse_profile)
        for staged in self.folder.glob("udb-upload-*"):
            reject_links(staged)
            shutil.rmtree(staged)
        self.proc = None
        self.ws = None
        self._cdp = None
        self._identity_contexts = {}
        self.tabs = {}
        self.frozen = False
        self.freeze_errors = 0
        self.deadline = time.monotonic() + _STARTUP_TIMEOUT
        try:
            endpoint = self.folder / "DevToolsActivePort"
            reject_links(endpoint)
            endpoint.unlink(missing_ok=True)
            exe = find_browser()
            self.proc = OwnedProcess(
                _browser_launch_args(exe, self.folder, visible),
                self.folder,
                LIMITS["browser_memory_mib"],
            )
            port, path = _browser_endpoint(endpoint, self.proc, _ENDPOINT_TIMEOUT)
            from websocket import create_connection

            self.ws = create_connection(
                "ws://127.0.0.1:" + port + path,
                timeout=10,
                suppress_origin=True,
                http_no_proxy=["127.0.0.1"],
            )
            self._cdp = CDPChannel(self.ws, self.proc)
            self.call("Browser.setDownloadBehavior", dict(behavior="deny"))
            # Adopt only initial targets in THIS newly created browser/profile.
            for target in self.call("Target.getTargets")["targetInfos"]:
                if target["type"] == "page":
                    self.attach(target["targetId"])
            self.deadline = None
        except BaseException:
            self.close()
            raise

    def call(self, method, params=None, session=None, timeout=10):
        if not self.proc or not self.proc.alive():
            raise PolicyError("Owned browser has exited")
        if self.deadline:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise PolicyError(
                    "Browser action budget exceeded; inspect before retrying"
                )
        return self._cdp.call(method, params, session, timeout)

    def attach(self, target):
        sid = self.call("Target.attachToTarget", dict(targetId=target, flatten=True))[
            "sessionId"
        ]
        self.tabs[target] = sid
        try:
            self.call("Page.enable", session=sid)
            self.call(
                "Page.addScriptToEvaluateOnNewDocument",
                dict(source=self.identity_script(), worldName="UDBIdentity"),
                session=sid,
            )
            self.call("Network.enable", session=sid)
            self.call(
                "Network.setBlockedURLs",
                dict(urls=list(_BLOCKED_URL_PATTERNS)),
                session=sid,
            )
            self.call("Page.setDownloadBehavior", dict(behavior="deny"), sid)
        except BaseException:
            self.tabs.pop(target, None)
            try:
                self.call("Target.detachFromTarget", dict(sessionId=sid), timeout=2)
            except Exception:
                pass
            raise
        return target

    def sid(self, tab):
        if tab not in self.tabs:
            raise PolicyError("Unknown or foreign tab; use host browser list")
        return self.tabs[tab]

    def evaluate(self, tab, expression, timeout=10, *, await_promise=False):
        r = self.call(
            "Runtime.evaluate",
            dict(
                expression=expression,
                returnByValue=True,
                awaitPromise=bool(await_promise),
                timeout=max(100, min(10000, int(timeout * 1000))),
            ),
            self.sid(tab),
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            raise PolicyError("Page operation failed; inspect page state")
        return r.get("result", {}).get("value")

    @staticmethod
    def _is_transient_context_error(exc):
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "cannot find context",
                "execution context was destroyed",
                "no frame with given id",
                "inspected target navigated or closed",
            )
        )

    def _wait_for_document_ready(
        self,
        tab,
        timeout_ms=10000,
        previous_url=None,
        require_transition=False,
        expected_loader_id=None,
    ):
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 100 <= timeout_ms <= 15000
        ):
            raise PolicyError("Navigation timeout_ms must be 100..15000")
        deadline = time.monotonic() + timeout_ms / 1000
        transition_seen = not require_transition
        expression = "({readyState:document.readyState,href:location.href})"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PolicyError(
                    f"Navigation timed out after {timeout_ms} ms; inspect before retrying"
                )
            # Same-URL navigations cannot be detected by comparing href. CDP
            # returns the new loaderId; wait until the main frame adopts it.
            if expected_loader_id and not transition_seen:
                try:
                    tree = self.call(
                        "Page.getFrameTree",
                        session=self.sid(tab),
                        timeout=max(0.2, min(1.0, remaining)),
                    )
                except PolicyError as exc:
                    if (
                        self._is_transient_context_error(exc)
                        or "timed out" in str(exc).lower()
                    ):
                        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                        continue
                    raise
                frame = tree.get("frameTree", {}).get("frame", {})
                if frame.get("loaderId") != expected_loader_id:
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                transition_seen = True
            try:
                state = self.evaluate(
                    tab, expression, timeout=max(0.2, min(1.0, remaining))
                )
            except PolicyError as exc:
                if (
                    self._is_transient_context_error(exc)
                    or "browser operation timed out" in str(exc).lower()
                ):
                    transition_seen = (
                        transition_seen or self._is_transient_context_error(exc)
                    )
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                raise
            if not isinstance(state, dict):
                raise PolicyError("Browser returned an invalid navigation state")
            current_url = str(state.get("href", ""))
            if previous_url is not None and current_url != previous_url:
                transition_seen = True
            if state.get("readyState") == "loading":
                transition_seen = True
            if not transition_seen:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue
            if current_url:
                try:
                    validate_url(current_url)
                except PolicyError as exc:
                    try:
                        self.call("Page.stopLoading", session=self.sid(tab), timeout=1)
                    except Exception:
                        pass
                    parsed = urlsplit(current_url)
                    if parsed.scheme in ("http", "https"):
                        raise PolicyError(
                            "Navigation reached a blocked private URL"
                        ) from exc
                    raise PolicyError(
                        "Navigation failed outside the allowed HTTP(S) URL space"
                    ) from exc
            if state.get("readyState") == "complete":
                if current_url == "about:blank":
                    safe_url = current_url
                else:
                    parsed = urlsplit(current_url)
                    safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                return dict(
                    navigation_completed=True,
                    ready_state="complete",
                    url=safe_url,
                )
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _wait_for_click_navigation(
        self,
        tab,
        previous_url,
        observe_ms=1000,
        timeout_ms=10000,
        previous_loader_id=None,
    ):
        if not isinstance(previous_url, str) or not previous_url:
            return dict(navigation_completed=False)
        observation_ms = max(100, min(1000, int(observe_ms)))
        deadline = time.monotonic() + observation_ms / 1000
        # Runtime.evaluate may lose its reply while a click destroys the old
        # execution context. Observe navigation from the browser target instead.
        transitioned = False
        next_loader_id = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                targets = self.call(
                    "Target.getTargets",
                    timeout=max(0.2, min(0.5, remaining)),
                ).get("targetInfos", [])
            except PolicyError as exc:
                if "timed out" not in str(exc).lower():
                    raise
                continue
            target = next((t for t in targets if t.get("targetId") == tab), None)
            if target is None:
                raise PolicyError("Owned browser tab closed during click")
            current_url = str(target.get("url", ""))
            if current_url and current_url != previous_url:
                transitioned = True
                break
            if previous_loader_id:
                try:
                    frame = (
                        self.call(
                            "Page.getFrameTree",
                            session=self.sid(tab),
                            timeout=max(0.2, min(0.5, deadline - time.monotonic())),
                        )
                        .get("frameTree", {})
                        .get("frame", {})
                    )
                except PolicyError as exc:
                    if (
                        self._is_transient_context_error(exc)
                        or "timed out" in str(exc).lower()
                    ):
                        continue
                    raise
                loader = frame.get("loaderId")
                if loader and loader != previous_loader_id:
                    next_loader_id = loader
                    transitioned = True
                    break
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if not transitioned:
            return dict(navigation_completed=False)
        try:
            return self._wait_for_document_ready(
                tab,
                timeout_ms=timeout_ms,
                previous_url=previous_url,
                require_transition=True,
                expected_loader_id=next_loader_id,
            )
        except PolicyError as exc:
            # The click already happened. Report an observed navigation timeout
            # without inviting the caller to repeat a potentially destructive click.
            if "navigation timed out" in str(exc).lower():
                return dict(
                    navigation_completed=False,
                    navigation_timed_out=True,
                )
            raise

    @staticmethod
    def _safe_page_url(value):
        if not isinstance(value, str):
            return ""
        if value == "about:blank":
            return value
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return ""
            validate_url(value)
        except (ValueError, PolicyError):
            return ""
        # Query strings and fragments may contain session tokens or private data.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def listing(self):
        targets = self.call("Target.getTargets")["targetInfos"]
        page_targets = [t for t in targets if t.get("type") == "page"]
        valid = {t["targetId"] for t in page_targets}
        for tab in list(self.tabs):
            if tab not in valid:
                self.tabs.pop(tab)
                getattr(self, "_identity_contexts", {}).pop(tab, None)
        # Only HTTP(S) pages in our own profile are interactive. Do not adopt
        # browser-internal pages or targets with an unsupported destination URL.
        for target in page_targets:
            tab = target["targetId"]
            safe_url = self._safe_page_url(target.get("url", ""))
            if not safe_url:
                self.tabs.pop(tab, None)
                getattr(self, "_identity_contexts", {}).pop(tab, None)
                try:
                    self.call("Target.closeTarget", dict(targetId=tab), timeout=2)
                except Exception:
                    pass
                continue
            if tab in self.tabs:
                continue
            # Popups created by pages in our private profile are adopted up to
            # the same cap as explicitly opened tabs.
            if len(self.tabs) >= LIMITS["max_tabs"]:
                try:
                    self.call("Target.closeTarget", dict(targetId=tab), timeout=2)
                except Exception:
                    pass
                continue
            try:
                self.attach(tab)
            except Exception:
                try:
                    self.call("Target.closeTarget", dict(targetId=tab), timeout=2)
                except Exception:
                    pass
        return [
            dict(
                tab_id=t["targetId"],
                title=t.get("title", "")[:500],
                url=self._safe_page_url(t.get("url", "")),
                owner="UDB",
                background=True,
            )
            for t in page_targets
            if t["targetId"] in self.tabs and self._safe_page_url(t.get("url", ""))
        ]

    def new(self, url="about:blank"):
        validate_url(url)
        if len(self.listing()) >= LIMITS["max_tabs"]:
            raise PolicyError("Maximum 4 owned browser tabs; close one first")
        target = self.call(
            "Target.createTarget", dict(url="about:blank", background=True)
        )["targetId"]
        self.attach(target)
        ready = self.navigate(target, url) if url != "about:blank" else {}
        return dict(ready, tab_id=target, owner="UDB", background=True)

    def navigate(self, tab, url, timeout_ms=10000):
        validate_url(url)
        try:
            previous_url = self.evaluate(tab, "location.href", timeout=1)
        except PolicyError:
            previous_url = None
        result = self.call("Page.navigate", dict(url=url), self.sid(tab))
        if result.get("errorText"):
            raise PolicyError("Navigation failed: " + result["errorText"][:200])
        if result.get("isDownload"):
            raise PolicyError("Downloads are blocked in the background browser")
        ready = self._wait_for_document_ready(
            tab,
            timeout_ms=timeout_ms,
            previous_url=previous_url,
            require_transition=bool(result.get("loaderId")),
            expected_loader_id=(
                result.get("loaderId") if previous_url == url else None
            ),
        )
        return dict(tab_id=tab, navigation_started=True, **ready)

    def read(self, tab):
        return read_browser(self, tab)

    @staticmethod
    def _locator_expression(
        selector, role, name, exact, body, visible_only=True, unique=True
    ):
        validate_locator(selector, role, name, exact)
        return (
            LOCATOR_SCRIPT.replace("__TRAVERSAL__", DOM_TRAVERSAL_SCRIPT)
            .replace("__SELECTOR__", json.dumps(selector))
            .replace("__ROLE__", json.dumps(role))
            .replace("__NAME__", json.dumps(name))
            .replace("__EXACT__", "true" if exact else "false")
            .replace("__VISIBLE_ONLY__", "true" if visible_only else "false")
            .replace("__UNIQUE__", "true" if unique else "false")
            .replace("__BODY__", body)
        )

    def snapshot(self, tab):
        return snapshot_browser(self, tab)

    def _css_selector_valid(self, tab, selector):
        """Return True/False for a CSS selector, or None if the page cannot be inspected."""
        if not selector:
            return True
        expression = (
            "(() => { try { document.querySelectorAll("
            + json.dumps(selector)
            + "); return true; } catch (_) { return false; } })()"
        )
        try:
            result = self.evaluate(tab, expression, timeout=1)
        except PolicyError:
            return None
        return result if isinstance(result, bool) else None

    def _wait_for_element_action(
        self, tab, expression, timeout_ms=5000, selector="", retry_context=True
    ):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(ok=False, error="timeout", timed_out=True)
            try:
                result = self.evaluate(
                    tab, expression, timeout=max(0.2, min(1.0, remaining))
                )
            except PolicyError as exc:
                if self._is_transient_context_error(exc):
                    if not retry_context:
                        raise PolicyError(
                            "Browser action outcome unknown; inspect before retrying"
                        ) from exc
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                if selector and self._css_selector_valid(tab, selector) is False:
                    return dict(ok=False, error="invalid_selector")
                raise
            if not isinstance(result, dict):
                return dict(ok=False, error="invalid_page_result")
            if result.get("ok"):
                return result
            error = result.get("error", "")
            matches = result.get("matches", 0)
            if error == "locator_not_unique" and matches > 1:
                return result
            if error in {"invalid_selector", "locator_search_truncated"}:
                return result
            if error == "state_mismatch" and not retry_context:
                return result  # An input/change handler already ran; never replay it.
            if error in {
                "locator_not_unique",
                "not_actionable",
                "state_mismatch",
            }:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(
                        ok=False,
                        error="timeout",
                        timed_out=True,
                        reason=result.get("reason") or error,
                    )
                time.sleep(min(0.1, remaining))
                continue
            return result

    def identity_script(self):
        return identity_script(getattr(self, "visible", False))

    def _show_identity(self, tab, x=None, y=None):
        """Install identity once per document and cache its isolated-world context."""
        session = self.sid(tab)
        contexts = getattr(self, "_identity_contexts", None)
        if contexts is None:
            contexts = {}
            self._identity_contexts = contexts
        context_id = contexts.get(tab)
        if context_id is not None:
            if x is None or y is None:
                return True
            try:
                expression = (
                    "globalThis.__udbMovePointer("
                    + json.dumps(float(x))
                    + ","
                    + json.dumps(float(y))
                    + ");"
                )
                result = self.call(
                    "Runtime.evaluate",
                    dict(
                        expression=expression,
                        contextId=context_id,
                        returnByValue=True,
                    ),
                    session,
                    timeout=2,
                )
                if result.get("exceptionDetails"):
                    raise PolicyError("Browser identity context is unavailable")
                return True
            except PolicyError as exc:
                if not self._is_transient_context_error(exc):
                    return False
                contexts.pop(tab, None)
        try:
            frame = self.call("Page.getFrameTree", session=session)["frameTree"][
                "frame"
            ]["id"]
            context_id = self.call(
                "Page.createIsolatedWorld",
                dict(frameId=frame, worldName="UDBIdentity"),
                session,
            )["executionContextId"]
            expression = self.identity_script()
            if x is not None and y is not None:
                expression += (
                    ";globalThis.__udbMovePointer("
                    + json.dumps(float(x))
                    + ","
                    + json.dumps(float(y))
                    + ");"
                )
            result = self.call(
                "Runtime.evaluate",
                dict(
                    expression=expression,
                    contextId=context_id,
                    returnByValue=True,
                ),
                session,
                timeout=2,
            )
            if result.get("exceptionDetails"):
                raise PolicyError("Browser identity installation failed")
            contexts[tab] = context_id
        except (PolicyError, KeyError):
            return False
        return True

    def element(
        self, tab, operation, selector="", value="", role="", name="", exact=True
    ):
        if operation not in ("click", "fill", "select_option", "check", "uncheck"):
            raise PolicyError("Unsupported browser element action")
        if not isinstance(value, str) or len(value) > 16000:
            raise PolicyError("Text exceeds limit")
        if operation == "select_option" and len(value) > 500:
            raise PolicyError("select_option requires an option value or label")

        if operation == "click":
            try:
                previous_url = self.evaluate(tab, "location.href", timeout=1)
            except PolicyError:
                previous_url = None
            if not isinstance(previous_url, str):
                previous_url = None
            body = (
                """if(e.tagName==='OPTION')return {ok:false,error:'use_select_option',action:'select_option'};
                if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||isInert(e))return {ok:false,error:'disabled'};
"""
                + CLICK_POINT_SCRIPT
                + """              return {ok:true,x:point[0],y:point[1]};"""
            )
            expression = self._locator_expression(selector, role, name, exact, body)
            result = self._wait_for_element_action(tab, expression, selector=selector)
            if not result.get("ok"):
                return result
            previous_loader_id = None
            try:
                previous_loader_id = (
                    self.call("Page.getFrameTree", session=self.sid(tab), timeout=1)
                    .get("frameTree", {})
                    .get("frame", {})
                    .get("loaderId")
                )
            except PolicyError:
                pass  # URL observation is still available if a frame is transitioning.
            self._mouse_click(tab, result["x"], result["y"])
            navigation = self._wait_for_click_navigation(
                tab, previous_url, previous_loader_id=previous_loader_id
            )
            return dict(ok=True, clicked=True, replay_safe=False, **navigation)

        select_body = SELECT_OPTION_SCRIPT.replace("__VALUE__", json.dumps(value))
        if operation == "fill":
            body = (
                "if(e.tagName==='SELECT'){"
                + select_body
                + "}\n"
                + """if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||isInert(e))return {ok:false,error:'disabled'};
              if(e.readOnly)return {ok:false,error:'readonly'};
              if(e.isContentEditable){e.focus();e.textContent=__VALUE__;}else{
                if(!['INPUT','TEXTAREA'].includes(e.tagName)||e.type==='file'||['button','submit','reset','checkbox','radio','hidden','image'].includes((e.type||'').toLowerCase()))return {ok:false,error:'unsupported_control'};
                e.focus();const p=e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,__VALUE__);}
              e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true};""".replace(
                    "__VALUE__", json.dumps(value)
                )
            )
        elif operation == "select_option":
            body = select_body
        else:
            desired = operation == "check"
            body = (
                """const native=e.tagName==='INPUT'&&['checkbox','radio'].includes((e.type||'').toLowerCase());const role=(e.getAttribute('role')||'').toLowerCase();
              if(!native&&!['checkbox','radio'].includes(role))return {ok:false,error:'unsupported_control'};
              if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||isInert(e))return {ok:false,error:'disabled'};
              const current=native?!!e.checked:e.getAttribute('aria-checked')==='true';
              if(current===__DESIRED__)return {ok:true,checked:current,already_set:true};
              if((native&&e.type==='radio'||role==='radio')&&__DESIRED__===false)return {ok:false,error:'radio_cannot_be_unchecked'};
"""
                + CLICK_POINT_SCRIPT
                + """              return {ok:true,x:point[0],y:point[1],needs_click:true};""".replace(
                    "__DESIRED__", "true" if desired else "false"
                )
            )
            expression = self._locator_expression(selector, role, name, exact, body)
            result = self._wait_for_element_action(tab, expression, selector=selector)
            if not result.get("ok") or not result.get("needs_click"):
                return result
            self._mouse_click(tab, result["x"], result["y"])
            verify_body = """const native=e.tagName==='INPUT'&&['checkbox','radio'].includes((e.type||'').toLowerCase());
              const checked=native?!!e.checked:e.getAttribute('aria-checked')==='true';
              return {ok:checked===__DESIRED__,checked,error:checked===__DESIRED__?'':'state_mismatch'};""".replace(
                "__DESIRED__", "true" if desired else "false"
            )
            verified = self._wait_for_element_action(
                tab,
                self._locator_expression(selector, role, name, exact, verify_body),
                timeout_ms=1000,
                selector=selector,
            )
            if not verified.get("ok"):
                return verified
            return dict(ok=True, checked=verified["checked"])

        expression = self._locator_expression(selector, role, name, exact, body)
        return self._wait_for_element_action(
            tab, expression, selector=selector, retry_context=False
        )

    def wait_for(
        self,
        tab,
        selector="",
        role="",
        name="",
        exact=True,
        state="visible",
        value="",
        timeout_ms=5000,
    ):
        if state not in {"attached", "visible", "hidden", "enabled", "text"}:
            raise PolicyError(
                "wait_for state must be attached, visible, hidden, enabled or text"
            )
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise PolicyError("wait_for timeout_ms must be 100..10000")
        if state == "text":
            if not isinstance(value, str) or not 1 <= len(value) <= 2000:
                raise PolicyError("wait_for state=text requires value")
            if not selector and not role:
                selector = "body"
        elif value:
            raise PolicyError("value is only used with wait_for state=text")
        validate_locator(selector, role, name, exact)
        body = """return new Promise(resolve => {
          const waitState=__STATE__, wantedText=__VALUE__, timeout=__TIMEOUT__;
          let observer=null, interval=null, timer=null, finished=false;
          const observed=new WeakSet();
          const finish=result=>{
            if(finished)return;
            finished=true;
            if(observer)observer.disconnect();
            if(interval!==null)clearInterval(interval);
            if(timer!==null)clearTimeout(timer);
            resolve(result);
          };
          const observeRoots=()=>{
            if(!observer)return;
            for(const root of openShadowRoots){
              const target=root===document?document.documentElement:root;
              if(target&&!observed.has(target)){
                observer.observe(target,{subtree:true,childList:true,attributes:true,characterData:true});
                observed.add(target);
              }
            }
          };
          const check=()=>{
            matches=findMatches();
            if(matches===null){finish({ok:false,error:'invalid_selector',state:waitState});return;}
            observeRoots();
            const visibleMatches=matches.filter(isVisible);
            const enabledMatches=visibleMatches.filter(e=>!e.matches(':disabled')&&e.getAttribute('aria-disabled')!=='true'&&!isInert(e));
            const textMatches=matches.filter(e=>String(e.innerText||e.textContent||'').includes(wantedText));
            const ok=waitState==='attached'?matches.length>0:
              waitState==='visible'?visibleMatches.length>0:
              waitState==='hidden'?(matches.length===0||visibleMatches.length===0):
              waitState==='enabled'?enabledMatches.length>0:
              waitState==='text'?textMatches.length>0:false;
            if(ok){finish({ok:true,state:waitState,matches:matches.length,visible:visibleMatches.length,enabled:enabledMatches.length});return;}
            if(traversalTruncated&&matches.length===0){
              finish({ok:false,error:'locator_search_truncated',state:waitState});return;
            }
          };
          observer=new MutationObserver(check);
          timer=setTimeout(()=>finish({ok:false,timed_out:true,state:waitState}),Math.max(1,timeout-50));
          interval=setInterval(()=>{observeRoots();check();},200);
          check();
        });"""
        body = body.replace("__VALUE__", json.dumps(value)).replace(
            "__STATE__", json.dumps(state)
        ).replace("__TIMEOUT__", str(timeout_ms))
        expression = self._locator_expression(
            selector, role, name, exact, body, visible_only=False, unique=False
        )
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(ok=False, timed_out=True, state=state)
            try:
                result = self.evaluate(
                    tab,
                    expression,
                    timeout=min(10.25, remaining + 0.25),
                    await_promise=True,
                )
            except PolicyError as exc:
                if self._is_transient_context_error(exc):
                    if time.monotonic() >= deadline:
                        return dict(ok=False, timed_out=True, state=state)
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                    continue
                if selector and self._css_selector_valid(tab, selector) is False:
                    return dict(ok=False, error="invalid_selector", state=state)
                if "timed out" in str(exc).lower():
                    return dict(ok=False, timed_out=True, state=state)
                raise
            if not isinstance(result, dict):
                return dict(ok=False, error="invalid_page_result", state=state)
            if result.get("error") in {"invalid_selector", "locator_search_truncated"}:
                return dict(
                    ok=False,
                    error=result["error"],
                    state=state,
                    matches=result.get("matches", 0),
                )
            return result

    def history(self, tab, action):
        session = self.sid(tab)
        if action == "reload":
            self.call("Page.reload", dict(ignoreCache=False), session, timeout=5)
            return dict(ok=True, action=action)
        if action not in ("back", "forward"):
            raise PolicyError("Unsupported history action")
        history = self.call("Page.getNavigationHistory", session=session, timeout=5)
        entries = history.get("entries", [])
        index = history.get("currentIndex", 0) + (-1 if action == "back" else 1)
        if not 0 <= index < len(entries):
            return dict(ok=False, reason="no_history", action=action)
        entry = entries[index]
        self.call(
            "Page.navigateToHistoryEntry", dict(entryId=entry["id"]), session, timeout=5
        )
        safe = urlsplit(str(entry.get("url", "")))
        safe_url = (
            (safe.scheme + "://" + safe.netloc + safe.path)
            if safe.scheme in ("http", "https")
            else ""
        )
        return dict(ok=True, action=action, url=safe_url)

    def screenshot(self, tab):
        self._show_identity(tab)
        result = self.call(
            "Page.captureScreenshot",
            dict(format="png", captureBeyondViewport=False),
            self.sid(tab),
        )
        if len(result["data"]) > 8 * 1048576:
            raise PolicyError("Screenshot too large")
        return result["data"]

    def freeze(self, frozen):
        if self.frozen == frozen:
            return

        errors = 0
        for tab in list(self.tabs):
            try:
                if frozen:
                    self.call("Page.stopLoading", session=self.sid(tab), timeout=2)
                self.call(
                    "Page.setWebLifecycleState",
                    dict(state="frozen" if frozen else "active"),
                    self.sid(tab),
                    timeout=2,
                )
            except Exception:
                errors += 1
        self.freeze_errors = errors
        if not errors:
            self.frozen = frozen

    def close_tab(self, tab):
        self.sid(tab)
        result = self.call("Target.closeTarget", dict(targetId=tab))
        if result.get("success"):
            self.tabs.pop(tab)
            getattr(self, "_identity_contexts", {}).pop(tab, None)
        return result

    def switch_presentation(self, visible):
        """Local-owner handoff; restart our browser and invalidate old tab IDs."""
        if type(visible) is not bool:
            raise PolicyError("Invalid browser presentation")
        if self.visible == visible:
            return dict(visible=visible, changed=False)
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        urls = []
        for target in self.call("Target.getTargets")["targetInfos"]:
            if target.get("type") == "page":
                url = target.get("url", "about:blank")
                try:
                    validate_url(url)
                except PolicyError:
                    continue
                urls.append(url)
        cookies = self.call("Storage.getCookies").get("cookies", [])
        # Keep session cookies in memory across local handoff; never return or
        # log them. Persistent login remains opt-in in the local manager.
        allowed = {
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "httpOnly",
            "sameSite",
            "expires",
            "partitionKey",
        }
        cookies = [
            {
                key: value
                for key, value in cookie.items()
                if key in allowed and not (key == "expires" and value <= 0)
            }
            for cookie in cookies
            if not cookie.get("partitionKeyOpaque", False)
        ]
        folder, persistent = self.folder, self.persistent
        self.close(remove_profile=False)
        self.__init__(
            folder, persistent=persistent, visible=visible, reuse_profile=True
        )
        self.deadline = deadline
        if cookies:
            self.call("Storage.setCookies", {"cookies": cookies})
        # Chrome may restore profile tabs itself. Normalize that inventory
        # before reopening URLs, otherwise each handoff duplicates tabs.
        targets = list(self.tabs)
        if not targets:
            raise PolicyError(
                "Browser restarted without a page; inspect its local window"
            )
        first = targets[0]
        for tab in targets[1:]:
            self.close_tab(tab)
        urls = [url for url in urls if url != "about:blank"][: LIMITS["max_tabs"]] or [
            "about:blank"
        ]
        self.navigate(first, urls[0])
        for url in urls[1:]:
            self.new(url)
        return dict(visible=visible, changed=True, tab_ids_invalidated=True)

    def close(self, remove_profile=True):
        if (
            getattr(self, "ws", None)
            and getattr(self, "proc", None)
            and self.proc.alive()
        ):
            try:
                self.deadline = None
                self.call("Browser.close", timeout=2)
            except Exception:
                pass  # Expected websocket closure; the owned job is still closed below.
            until = time.monotonic() + 2
            while self.proc.alive() and time.monotonic() < until:
                time.sleep(0.05)
        if self.ws:
            try:
                self.ws.close(timeout=1)
            except Exception:
                pass  # The socket may already be closed; continue owned-process cleanup.
            self.ws = None
        self._cdp = None
        getattr(self, "_identity_contexts", {}).clear()
        if self.proc:
            self.proc.close()
            self.proc = None
        for staged in self.folder.glob("udb-upload-*"):
            reject_links(staged)
            shutil.rmtree(staged)
        if (
            remove_profile
            and not getattr(self, "persistent", False)
            and self.folder.exists()
        ):
            for _ in range(6):
                try:
                    shutil.rmtree(self.folder)
                    break
                except OSError:
                    time.sleep(0.1)
