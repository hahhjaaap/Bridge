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
    u = urlsplit(value)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise PolicyError("Only HTTP(S) pages without embedded credentials")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise PolicyError("Local browser endpoints are private")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PolicyError("Private IP navigation blocked")
    return value


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
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
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
        self.call("Page.enable", session=sid)
        self.call("Emulation.setCPUThrottlingRate", dict(rate=2), sid)
        self.call("Page.setDownloadBehavior", dict(behavior="deny"), sid)
        return target

    def sid(self, tab):
        if tab not in self.tabs:
            raise PolicyError("Unknown or foreign tab; use host browser list")
        return self.tabs[tab]

    def evaluate(self, tab, expression):
        r = self.call(
            "Runtime.evaluate",
            dict(expression=expression, returnByValue=True, timeout=5000),
            self.sid(tab),
        )
        if r.get("exceptionDetails"):
            raise PolicyError("Page operation failed; inspect page state")
        return r.get("result", {}).get("value")

    def listing(self):
        targets = self.call("Target.getTargets")["targetInfos"]
        valid = {t["targetId"] for t in targets if t["type"] == "page"}
        for t in list(self.tabs):
            if t not in valid:
                self.tabs.pop(t)
        # Popups are capped, never silently adopted for interaction.
        for t in targets:
            if t["type"] == "page" and t["targetId"] not in self.tabs:
                self.call("Target.closeTarget", dict(targetId=t["targetId"]))
        return [
            dict(
                tab_id=t["targetId"],
                title=t.get("title", "")[:500],
                url=t.get("url", "")[:4096],
            )
            for t in targets
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

    def navigate(self, tab, url):
        validate_url(url)
        result = self.call("Page.navigate", dict(url=url), self.sid(tab))
        if result.get("errorText"):
            raise PolicyError("Navigation failed: " + result["errorText"][:200])
        return dict(tab_id=tab, navigation_started=True)

    def read(self, tab):
        return self.evaluate(
            tab,
            """(() => ({url:location.href,title:document.title,
          text:(document.body?.innerText||'').slice(0,20000),
          elements:Array.from(document.querySelectorAll('a,button,input,textarea,select,[role=button]')).slice(0,120).map(e=>({
            tag:e.tagName,id:e.id,name:e.getAttribute('name'),type:e.getAttribute('type'),
            text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,200),
            value:e.type==='password'?'[redacted]':String(e.value||'').slice(0,300)}))}))()""",
        )

    def element(self, tab, operation, selector, value=""):
        if operation not in ("click", "fill"):
            raise PolicyError("Unsupported browser element action")
        if not isinstance(selector, str) or not 1 <= len(selector) <= 500:
            raise PolicyError("Provide a CSS selector")
        if not isinstance(value, str) or len(value) > 16000:
            raise PolicyError("Text exceeds limit")
        return self.evaluate(
            tab,
            """(() => {
          const a=document.querySelectorAll(%s); if(a.length!==1) return {ok:false,error:'selector_not_unique',matches:a.length};
          const e=a[0]; if(e.disabled||e.readOnly) return {ok:false,error:'disabled_or_readonly'};
          if(%s==='fill') {
            if(!['INPUT','TEXTAREA'].includes(e.tagName)||e.type==='file') return {ok:false,error:'unsupported_control'};
            const p=e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype;
            Object.getOwnPropertyDescriptor(p,'value').set.call(e,%s);
            e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
          } else e.click();
          return {ok:true};
        })()"""
            % (json.dumps(selector), json.dumps(operation), json.dumps(value)),
        )

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
