"""A browser owned by UDB, with a fresh profile and no visible/focusable window.

Never attaches to the user's browser, discovers their tabs or injects OS input.
All CDP verbs stay inside the exact process/profile launched here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .core import PolicyError, reject_links
from .resources import LIMITS


def _job_locked(method):
    def wrapped(self, *args, **kwargs):
        with self._job_lock:
            return method(self, *args, **kwargs)

    return wrapped


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
    if host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal")
    ):
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
        "button", "link", "textbox", "searchbox", "checkbox", "radio",
        "combobox", "listbox", "option", "heading", "tab", "menuitem",
        "switch", "slider", "spinbutton", "treeitem",
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
_KEY_SPECIAL = {
    "enter": ("Enter", "Enter", 13), "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27), "esc": ("Escape", "Escape", 27),
    "space": (" ", "Space", 32), "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowup": ("ArrowUp", "ArrowUp", 38), "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39), "home": ("Home", "Home", 36),
    "end": ("End", "End", 35), "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34), "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
}
_KEY_COMBOS = frozenset({
    "CTRL+A", "CONTROL+A", "CTRL+ENTER", "CONTROL+ENTER", "SHIFT+TAB",
    "CTRL+BACKSPACE", "CONTROL+BACKSPACE", "CTRL+ARROWLEFT", "CONTROL+ARROWLEFT",
    "CTRL+ARROWRIGHT", "CONTROL+ARROWRIGHT", "CTRL+HOME", "CONTROL+HOME",
    "CTRL+END", "CONTROL+END",
})


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


def parse_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise PolicyError("Provide a supported browser key")
    parts = value.split("+")
    aliases = {"ctrl": "CTRL", "control": "CONTROL", "shift": "SHIFT", "alt": "ALT", "meta": "META"}
    modifiers = []
    for part in parts[:-1]:
        token = aliases.get(part.lower())
        if token is None or token in modifiers:
            raise PolicyError("Unsupported browser key modifier")
        modifiers.append(token)
    final = parts[-1]
    info = _KEY_SPECIAL.get(final.lower())
    if info is None and len(final) == 1 and final.isalpha():
        info = (final.lower(), "Key" + final.upper(), ord(final.upper()))
    elif info is None and len(final) == 1 and final.isdigit():
        info = (final, "Digit" + final, ord(final))
    if info is None:
        raise PolicyError("Unsupported browser key")
    if modifiers and "+".join(modifiers + [final.upper()]) not in _KEY_COMBOS:
        raise PolicyError("That browser key combination is not allowed")
    mask = 0
    for modifier in modifiers:
        mask |= {"ALT": 1, "CTRL": 2, "CONTROL": 2, "META": 4, "SHIFT": 8}[modifier]
    key, code, vk = info
    text = key if not modifiers and (len(final) == 1 or final.lower() == "space") else ""
    return dict(key=key, code=code, vk=vk, modifiers=mask, text=text)


class OwnedProcess:
    """Windows Job Object: membership is captured at spawn, never trusted PIDs."""

    def __init__(self, argv, cwd, memory_mib=0):
        self._job_lock = threading.RLock()
        import win32api
        import win32con
        import win32job
        import win32process

        self.job = win32job.CreateJobObject(None, "Local\\UDB-" + uuid.uuid4().hex)
        self.handle = None
        try:
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            info["BasicLimitInformation"]["ActiveProcessLimit"] = (
                LIMITS["max_browser_processes"] if memory_mib else 2
            )
            if memory_mib:
                info["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
                )
                info["JobMemoryLimit"] = memory_mib * 1048576
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, info
            )
            if memory_mib:
                # CPU hard cap applies to the entire owned browser process tree.
                import ctypes
                from ctypes import wintypes as w

                class CpuRate(ctypes.Structure):
                    _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

                cpu = CpuRate(1 | 4, 2500)  # ENABLE | HARD_CAP, 25% of machine CPU
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.SetInformationJobObject.argtypes = [
                    w.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    w.DWORD,
                ]
                if not k.SetInformationJobObject(
                    int(self.job), 15, ctypes.byref(cpu), ctypes.sizeof(cpu)
                ):
                    raise OSError(
                        ctypes.get_last_error(), "Browser CPU cap unavailable"
                    )
            flags = (
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW | 0x00004000
            )  # BELOW_NORMAL_PRIORITY_CLASS
            self.handle, thread, self.pid, _ = win32process.CreateProcess(
                str(argv[0]),
                subprocess.list2cmdline(list(map(str, argv))),
                None,
                None,
                False,
                flags,
                None,
                str(cwd),
                win32process.STARTUPINFO(),
            )
            try:
                win32job.AssignProcessToJobObject(self.job, self.handle)
                win32process.ResumeThread(thread)
            except BaseException:
                win32api.TerminateProcess(self.handle, 1)
                raise
            finally:
                thread.Close()
        except BaseException:
            self.close()
            raise

    @_job_locked
    def alive(self):
        if not self.handle:
            return False
        import win32process

        return win32process.GetExitCodeProcess(self.handle) == 259

    @_job_locked
    def usage(self):
        import win32job

        if not self.job:
            return dict(processes=0, peak_commit_mib=0)
        data = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation
        )
        memory = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation
        )
        cpu_cap = None
        if memory["JobMemoryLimit"]:
            import ctypes
            from ctypes import wintypes as w

            class CpuRate(ctypes.Structure):
                _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

            value = CpuRate()
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.QueryInformationJobObject.argtypes = [
                w.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                w.DWORD,
                ctypes.c_void_p,
            ]
            if (
                k.QueryInformationJobObject(
                    int(self.job), 15, ctypes.byref(value), ctypes.sizeof(value), None
                )
                and value.flags & 4
            ):
                cpu_cap = value.rate / 100
        return dict(
            processes=data["ActiveProcesses"],
            peak_commit_mib=round(memory["PeakJobMemoryUsed"] / 1048576, 1),
            memory_limit_mib=round(memory["JobMemoryLimit"] / 1048576),
            cpu_cap_percent=cpu_cap,
        )

    @_job_locked
    def owns_pid(self, pid):
        import win32api
        import win32job

        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                return bool(self.job and win32job.IsProcessInJob(handle, self.job))
            finally:
                handle.Close()
        except Exception:
            return False

    @_job_locked
    def close(self):
        # Closing this private job terminates only members created by us.
        if getattr(self, "job", None):
            self.job.Close()
            self.job = None
        if getattr(self, "handle", None):
            import win32event

            win32event.WaitForSingleObject(self.handle, 3000)
            self.handle.Close()
            self.handle = None


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


class Browser:
    def __init__(self, folder):
        self.folder = Path(folder)
        reject_links(self.folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.proc = None
        self.ws = None
        self.seq = 0
        self.tabs = {}
        self.frozen = False
        self.freeze_errors = 0
        self.deadline = time.monotonic() + 20
        try:
            exe = find_browser()
            self.proc = OwnedProcess(
                [
                    exe,
                    "--headless=new",
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                    "--user-data-dir=" + str(self.folder),
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-sync",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-component-update",
                    "--disk-cache-size=67108864",
                    "--media-cache-size=16777216",
                    "--disable-gpu",
                    "--mute-audio",
                    "--renderer-process-limit=4",
                    "--disable-features=Translate,MediaRouter",
                    "about:blank",
                ],
                self.folder,
                LIMITS["browser_memory_mib"],
            )
            endpoint = self.folder / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if not self.proc.alive():
                    raise PolicyError("Private browser exited during startup")
                if time.monotonic() > deadline:
                    raise PolicyError("Private browser startup timed out")
                time.sleep(0.05)
            port, path, *_ = endpoint.read_text("ascii").splitlines()
            if (
                not port.isdigit()
                or not path.startswith("/devtools/browser/")
                or not 1 <= int(port) <= 65535
            ):
                raise PolicyError("Invalid private browser endpoint")
            from websocket import create_connection

            self.ws = create_connection(
                "ws://127.0.0.1:" + port + path,
                timeout=10,
                suppress_origin=True,
                http_no_proxy=["127.0.0.1"],
            )
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
        self.seq += 1
        number = self.seq
        msg = dict(id=number, method=method, params=params or {})
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        self.ws.settimeout(min(1, timeout))
        from websocket import WebSocketTimeoutException

        while time.monotonic() < deadline:
            if not self.proc or not self.proc.alive():
                raise PolicyError("Owned browser was stopped")
            try:
                raw = self.ws.recv()
            except WebSocketTimeoutException:
                continue
            if not raw:
                raise PolicyError("Private browser connection closed")
            if len(raw) > 12 * 1048576:
                raise PolicyError("Browser reply exceeds limit")
            reply = json.loads(raw)
            if reply.get("id") != number:
                continue
            if "error" in reply:
                raise PolicyError(
                    "Browser operation failed: "
                    + str(reply["error"].get("message", "unknown"))[:300]
                )
            return reply.get("result", {})
        raise PolicyError(
            "Browser operation timed out; outcome may be unknown, inspect before retrying"
        )

    def attach(self, target):
        sid = self.call("Target.attachToTarget", dict(targetId=target, flatten=True))[
            "sessionId"
        ]
        self.tabs[target] = sid
        try:
            self.call("Page.enable", session=sid)
            self.call("Network.enable", session=sid)
            self.call(
                "Network.setBlockedURLs",
                dict(urls=list(_BLOCKED_URL_PATTERNS)),
                session=sid,
            )
            self.call("Emulation.setCPUThrottlingRate", dict(rate=2), sid)
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

    def evaluate(self, tab, expression, timeout=10):
        r = self.call(
            "Runtime.evaluate",
            dict(
                expression=expression,
                returnByValue=True,
                awaitPromise=False,
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
        self, tab, timeout_ms=10000, previous_url=None, require_transition=False
    ):
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 15000:
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
            try:
                state = self.evaluate(
                    tab, expression, timeout=max(0.2, min(1.0, remaining))
                )
            except PolicyError as exc:
                if self._is_transient_context_error(exc):
                    transition_seen = True
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

    def listing(self):
        targets = self.call("Target.getTargets")["targetInfos"]
        page_targets = [t for t in targets if t.get("type") == "page"]
        valid = {t["targetId"] for t in page_targets}
        for tab in list(self.tabs):
            if tab not in valid:
                self.tabs.pop(tab)
        # Pages created by links/popups are still inside our private profile.
        # Adopt them up to the same cap as explicitly opened tabs.
        for target in page_targets:
            tab = target["targetId"]
            if tab in self.tabs:
                continue
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
                url=t.get("url", "")[:4096],
            )
            for t in page_targets
            if t["targetId"] in self.tabs
        ]

    def new(self, url="about:blank"):
        validate_url(url)
        if len(self.listing()) >= LIMITS["max_tabs"]:
            raise PolicyError("Maximum 4 owned browser tabs; close one first")
        target = self.call(
            "Target.createTarget", dict(url="about:blank", background=True)
        )["targetId"]
        self.attach(target)
        if url != "about:blank":
            self.navigate(target, url)
        return dict(tab_id=target)

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
        )
        return dict(tab_id=tab, navigation_started=True, **ready)

    def read(self, tab):
        return self.evaluate(
            tab,
            """(() => {
              const textOf=e=>String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
              const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
                return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0;};
              return {url:location.origin==='null'?'about:blank':location.origin+location.pathname,
                title:String(document.title||'').slice(0,500),
                text:(document.body?.innerText||'').slice(0,20000),
                elements:Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,[role]'))
                  .filter(visible).slice(0,120).map(e=>{
                    const type=(e.getAttribute('type')||'').toLowerCase();
                    const secret=type==='password'||type==='file'||/password|secret|token|otp|one-time-code/i.test([e.autocomplete,e.name,e.id].join(' '));
                    return {tag:e.tagName,id:String(e.id||'').slice(0,120),name:e.getAttribute('name'),type,
                      role:e.getAttribute('role')||'',placeholder:String(e.getAttribute('placeholder')||'').slice(0,160),
                      text:textOf(e).slice(0,200),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
                      value:secret?'[redacted]':String(e.value||'').slice(0,300)};
                  })};
            })()""",
            timeout=5,
        )

    @staticmethod
    def _locator_expression(selector, role, name, exact, body, visible_only=True, unique=True):
        validate_locator(selector, role, name, exact)
        template = """(() => {
          const selector = __SELECTOR__, wantedRole = __ROLE__, wantedName = __NAME__, exact = __EXACT__;
          const isVisible = e => { const s=getComputedStyle(e),r=e.getBoundingClientRect();
            return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0; };
          const textOf = e => String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
          const roleOf = e => {
            const explicit=(e.getAttribute('role')||'').trim().split(/\\s+/)[0]; if(explicit)return explicit;
            const tag=e.tagName.toLowerCase(),type=(e.getAttribute('type')||'').toLowerCase();
            if(tag==='a'&&e.hasAttribute('href'))return 'link';
            if(tag==='button'||tag==='summary'||['button','submit','reset','image'].includes(type))return 'button';
            if(tag==='textarea'||e.isContentEditable)return 'textbox';
            if(tag==='select')return e.multiple?'listbox':'combobox';
            if(tag==='input'){if(type==='checkbox')return 'checkbox';if(type==='radio')return 'radio';
              if(type==='range')return 'slider';if(type==='number')return 'spinbutton';if(type==='search')return 'searchbox';
              if(type==='hidden')return 'none';if(type==='file')return 'button';return 'textbox';}
            if(tag==='option')return 'option';if(/^h[1-6]$/.test(tag))return 'heading';return '';
          };
          const nameOf = e => {
            const ids=(e.getAttribute('aria-labelledby')||'').trim().split(/\\s+/).filter(Boolean);
            const labelled=ids.map(id=>document.getElementById(id)).filter(Boolean).map(textOf).join(' ').trim();
            const labels=e.labels?Array.from(e.labels).map(textOf).filter(Boolean).join(' '):'';
            return String(labelled||e.getAttribute('aria-label')||labels||e.getAttribute('alt')||e.getAttribute('title')
              ||e.getAttribute('placeholder')||textOf(e)||e.getAttribute('name')||e.id||'').trim().slice(0,500);
          };
          let matches=[];
          if(selector){try{matches=Array.from(document.querySelectorAll(selector));}catch(_){return {ok:false,error:'invalid_selector'};}}
          else matches=Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,summary,[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6'))
            .filter(e=>roleOf(e)===wantedRole&&(!wantedName||(exact?nameOf(e)===wantedName:nameOf(e).includes(wantedName))));
          if(__VISIBLE_ONLY__)matches=matches.filter(isVisible);
          if(__UNIQUE__&&matches.length!==1)return {ok:false,error:'locator_not_unique',matches:matches.length};
          const e=matches[0]||null;
          __BODY__
        })()"""
        return (
            template.replace("__SELECTOR__", json.dumps(selector))
            .replace("__ROLE__", json.dumps(role))
            .replace("__NAME__", json.dumps(name))
            .replace("__EXACT__", "true" if exact else "false")
            .replace("__VISIBLE_ONLY__", "true" if visible_only else "false")
            .replace("__UNIQUE__", "true" if unique else "false")
            .replace("__BODY__", body)
        )

    def snapshot(self, tab):
        return self.evaluate(
            tab,
            """(() => {
              const textOf=e=>String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
              const roleOf=e=>{const x=(e.getAttribute('role')||'').trim().split(/\\s+/)[0];if(x)return x;
                const t=e.tagName.toLowerCase(),y=(e.getAttribute('type')||'').toLowerCase();
                if(t==='a'&&e.hasAttribute('href'))return 'link';if(t==='button'||t==='summary'||['button','submit','reset','image'].includes(y))return 'button';
                if(t==='textarea'||e.isContentEditable)return 'textbox';if(t==='select')return e.multiple?'listbox':'combobox';
                if(t==='input'){if(y==='checkbox')return 'checkbox';if(y==='radio')return 'radio';if(y==='range')return 'slider';if(y==='number')return 'spinbutton';if(y==='search')return 'searchbox';if(y==='hidden')return 'none';if(y==='file')return 'button';return 'textbox';}
                if(t==='option')return 'option';if(/^h[1-6]$/.test(t))return 'heading';return '';};
              const nameOf=e=>{const ids=(e.getAttribute('aria-labelledby')||'').trim().split(/\\s+/).filter(Boolean);
                const labelled=ids.map(id=>document.getElementById(id)).filter(Boolean).map(textOf).join(' ').trim();
                const labels=e.labels?Array.from(e.labels).map(textOf).filter(Boolean).join(' '):'';
                return String(labelled||e.getAttribute('aria-label')||labels||e.getAttribute('alt')||e.getAttribute('title')||e.getAttribute('placeholder')||textOf(e)||e.getAttribute('name')||e.id||'').trim().slice(0,240);};
              const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0;};
              const all=Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,summary,[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6')).filter(visible);
              const elements=all.slice(0,160).map((e,i)=>{const type=(e.getAttribute('type')||'').toLowerCase();
                const secret=type==='password'||type==='file'||/password|secret|token|otp|one-time-code/i.test([e.autocomplete,e.name,e.id].join(' '));
                let href='';try{const u=new URL(e.href,location.href);if(['http:','https:'].includes(u.protocol))href=u.origin+u.pathname;}catch(_){}
                return {index:i+1,role:roleOf(e),name:nameOf(e),tag:e.tagName.toLowerCase(),type,
                  id:String(e.id||'').slice(0,120),name_attr:String(e.getAttribute('name')||'').slice(0,120),
                  placeholder:String(e.getAttribute('placeholder')||'').slice(0,160),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
                  checked:typeof e.checked==='boolean'?e.checked:null,selected:typeof e.selected==='boolean'?e.selected:null,
                  value:secret?'[redacted]':String(e.value||'').slice(0,160),href};});
              return {url:location.origin==='null'?'about:blank':location.origin+location.pathname,title:String(document.title||'').slice(0,500),
                count:all.length,truncated:all.length>160,elements};
            })()""",
            timeout=5,
        )

    def _wait_for_element_action(self, tab, expression, timeout_ms=5000):
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
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                raise
            if not isinstance(result, dict):
                return dict(ok=False, error="invalid_page_result")
            if result.get("ok"):
                return result
            error = result.get("error", "")
            matches = result.get("matches", 0)
            if error == "locator_not_unique" and matches > 1:
                return result
            if error == "invalid_selector":
                return result
            if error in {
                "locator_not_unique",
                "not_actionable",
                "disabled",
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

    def _mouse_click(self, tab, x, y):
        session = self.sid(tab)
        point = dict(x=float(x), y=float(y))
        self.call(
            "Input.dispatchMouseEvent",
            dict(type="mouseMoved", **point),
            session,
            timeout=2,
        )
        press_error = None
        try:
            self.call(
                "Input.dispatchMouseEvent",
                dict(type="mousePressed", button="left", clickCount=1, **point),
                session,
                timeout=2,
            )
        except Exception as exc:
            press_error = exc
        finally:
            try:
                self.call(
                    "Input.dispatchMouseEvent",
                    dict(type="mouseReleased", button="left", clickCount=1, **point),
                    session,
                    timeout=2,
                )
            except Exception:
                if press_error is None:
                    raise
        if press_error is not None:
            raise press_error

    def element(self, tab, operation, selector="", value="", role="", name="", exact=True):
        if operation not in ("click", "fill", "select_option", "check", "uncheck"):
            raise PolicyError("Unsupported browser element action")
        if not isinstance(value, str) or len(value) > 16000:
            raise PolicyError("Text exceeds limit")
        if operation == "select_option" and (not value or len(value) > 500):
            raise PolicyError("select_option requires an option value or label")

        if operation == "click":
            body = """if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||e.closest('[inert]'))return {ok:false,error:'disabled'};
              e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              const s=getComputedStyle(e);if(s.pointerEvents==='none')return {ok:false,error:'not_actionable',reason:'pointer_events'};
              const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return {ok:false,error:'not_actionable',reason:'not_visible'};
              const cx=r.left+r.width/2,cy=r.top+r.height/2;
              const points=[[cx,cy],[r.left+Math.min(4,r.width/2),cy],[r.right-Math.min(4,r.width/2),cy],[cx,r.top+Math.min(4,r.height/2)],[cx,r.bottom-Math.min(4,r.height/2)]];
              const point=points.find(([x,y])=>x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(()=>{const top=document.elementFromPoint(x,y);return !!top&&(top===e||e.contains(top));})());
              if(!point)return {ok:false,error:'not_actionable',reason:'obscured'};
              return {ok:true,x:point[0],y:point[1]};"""
            expression = self._locator_expression(
                selector, role, name, exact, body
            )
            result = self._wait_for_element_action(tab, expression)
            if not result.get("ok"):
                return result
            self._mouse_click(tab, result["x"], result["y"])
            return dict(ok=True, clicked=True)

        if operation == "fill":
            body = """if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true')return {ok:false,error:'disabled'};
              if(e.readOnly)return {ok:false,error:'readonly'};
              if(e.isContentEditable){e.focus();e.textContent=__VALUE__;}else{
                if(!['INPUT','TEXTAREA'].includes(e.tagName)||e.type==='file'||['button','submit','reset','checkbox','radio','hidden','image'].includes((e.type||'').toLowerCase()))return {ok:false,error:'unsupported_control'};
                e.focus();const p=e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,__VALUE__);}
              e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true};""".replace(
                "__VALUE__", json.dumps(value)
            )
        elif operation == "select_option":
            body = """if(e.tagName!=='SELECT'||e.matches(':disabled')||e.getAttribute('aria-disabled')==='true')return {ok:false,error:'unsupported_control'};
              const wanted=__VALUE__;const option=Array.from(e.options).find(o=>o.value===wanted||o.label===wanted||String(o.textContent||'').trim()===wanted);
              if(!option||option.disabled)return {ok:false,error:'option_not_found'};e.value=option.value;
              e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
              return {ok:e.value===option.value,value:e.value,label:option.label||String(option.textContent||'').trim()};""".replace(
                "__VALUE__", json.dumps(value)
            )
        else:
            desired = operation == "check"
            body = """const native=e.tagName==='INPUT'&&['checkbox','radio'].includes((e.type||'').toLowerCase());const role=(e.getAttribute('role')||'').toLowerCase();
              if(!native&&!['checkbox','radio'].includes(role))return {ok:false,error:'unsupported_control'};
              if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||e.closest('[inert]'))return {ok:false,error:'disabled'};
              if((native&&e.type==='radio'||role==='radio')&&__DESIRED__===false)return {ok:false,error:'radio_cannot_be_unchecked'};
              const current=native?!!e.checked:e.getAttribute('aria-checked')==='true';
              if(current===__DESIRED__)return {ok:true,checked:current,already_set:true};
              e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              const s=getComputedStyle(e);if(s.pointerEvents==='none')return {ok:false,error:'not_actionable',reason:'pointer_events'};
              const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return {ok:false,error:'not_actionable',reason:'not_visible'};
              const cx=r.left+r.width/2,cy=r.top+r.height/2;
              const points=[[cx,cy],[r.left+Math.min(4,r.width/2),cy],[r.right-Math.min(4,r.width/2),cy],[cx,r.top+Math.min(4,r.height/2)],[cx,r.bottom-Math.min(4,r.height/2)]];
              const point=points.find(([x,y])=>x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(()=>{const top=document.elementFromPoint(x,y);return !!top&&(top===e||e.contains(top));})());
              if(!point)return {ok:false,error:'not_actionable',reason:'obscured'};
              return {ok:true,x:point[0],y:point[1],needs_click:true};""".replace(
                "__DESIRED__", "true" if desired else "false"
            )
            expression = self._locator_expression(
                selector, role, name, exact, body
            )
            result = self._wait_for_element_action(tab, expression)
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
                self._locator_expression(
                    selector, role, name, exact, verify_body
                ),
                timeout_ms=1000,
            )
            if not verified.get("ok"):
                return verified
            return dict(ok=True, checked=verified["checked"])

        expression = self._locator_expression(
            selector, role, name, exact, body
        )
        return self._wait_for_element_action(tab, expression)

    def wait_for(self, tab, selector="", role="", name="", exact=True, state="visible", value="", timeout_ms=5000):
        if state not in {"attached","visible","hidden","enabled","text"}:
            raise PolicyError("wait_for state must be attached, visible, hidden, enabled or text")
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise PolicyError("wait_for timeout_ms must be 100..10000")
        if state=="text":
            if not isinstance(value,str) or not 1 <= len(value) <= 2000:
                raise PolicyError("wait_for state=text requires value")
            if not selector and not role:
                selector="body"
        elif value:
            raise PolicyError("value is only used with wait_for state=text")
        validate_locator(selector,role,name,exact)
        body="""const visibleMatches=matches.filter(isVisible);const enabledMatches=visibleMatches.filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true');
          const textMatches=matches.filter(e=>String(e.innerText||e.textContent||'').includes(__VALUE__));const state=__STATE__;
          const ok=state==='attached'?matches.length>0:state==='visible'?visibleMatches.length>0:state==='hidden'?(matches.length===0||visibleMatches.length===0):state==='enabled'?enabledMatches.length>0:state==='text'?textMatches.length>0:false;
          return {ok,state,matches:matches.length,visible:visibleMatches.length,enabled:enabledMatches.length};"""
        body=body.replace("__VALUE__",json.dumps(value)).replace("__STATE__",json.dumps(state))
        expression=self._locator_expression(selector,role,name,exact,body,visible_only=False,unique=False)
        deadline=time.monotonic()+timeout_ms/1000
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                return dict(ok=False,timed_out=True,state=state)
            try:
                result=self.evaluate(tab,expression,timeout=max(0.2,min(1.5,remaining)))
            except PolicyError as exc:
                if self._is_transient_context_error(exc):
                    if time.monotonic() >= deadline:
                        return dict(ok=False, timed_out=True, state=state)
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                if time.monotonic() >= deadline:
                    return dict(ok=False, timed_out=True, state=state)
                raise
            if result.get("error")=="invalid_selector":
                return dict(ok=False,error="invalid_selector",state=state)
            if result.get("ok"):
                return result
            if time.monotonic()>=deadline:
                return dict(ok=False,timed_out=True,state=state,matches=result.get("matches",0))
            time.sleep(min(0.1,max(0,deadline-time.monotonic())))

    def press(self, tab, key, selector="", role="", name="", exact=True):
        info=parse_key(key)
        body="""if(e.disabled||e.getAttribute('aria-disabled')==='true')return {ok:false,error:'disabled'};
          if(typeof e.focus!=='function')return {ok:false,error:'not_focusable'};e.focus();
          return {ok:document.activeElement===e||e.contains(document.activeElement),error:'not_focusable'};"""
        focused=self.evaluate(tab,self._locator_expression(selector,role,name,exact,body))
        if not focused.get("ok"):
            return focused
        session=self.sid(tab)
        params=dict(key=info["key"],code=info["code"],windowsVirtualKeyCode=info["vk"],modifiers=info["modifiers"])
        down_params=dict(params)
        if info["text"]:
            down_params["text"]=info["text"]
            down_params["unmodifiedText"]=info["text"]
        self.call("Input.dispatchKeyEvent",dict(type="keyDown",**down_params),session,timeout=2)
        self.call("Input.dispatchKeyEvent",dict(type="keyUp",**params),session,timeout=2)
        return dict(ok=True,key=key,focused=True)

    def history(self, tab, action):
        session=self.sid(tab)
        if action=="reload":
            self.call("Page.reload",dict(ignoreCache=False),session,timeout=5)
            return dict(ok=True,action=action)
        if action not in ("back","forward"):
            raise PolicyError("Unsupported history action")
        history=self.call("Page.getNavigationHistory",session=session,timeout=5)
        entries=history.get("entries",[])
        index=history.get("currentIndex",0)+(-1 if action=="back" else 1)
        if not 0<=index<len(entries):
            return dict(ok=False,reason="no_history",action=action)
        entry=entries[index]
        self.call("Page.navigateToHistoryEntry",dict(entryId=entry["id"]),session,timeout=5)
        safe=urlsplit(str(entry.get("url","")))
        safe_url=(safe.scheme+"://"+safe.netloc+safe.path) if safe.scheme in ("http","https") else ""
        return dict(ok=True,action=action,url=safe_url)

    def screenshot(self, tab):
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
        return result

    def close(self):
        if self.ws:
            try:
                self.ws.close(timeout=1)
            except Exception:
                pass  # The socket may already be closed; continue owned-process cleanup.
            self.ws = None
        if self.proc:
            self.proc.close()
            self.proc = None
        if self.folder.exists():
            for _ in range(6):
                try:
                    shutil.rmtree(self.folder)
                    break
                except OSError:
                    time.sleep(0.1)