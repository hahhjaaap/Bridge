"""GUI-owned host engine: one serial action worker and a 2-second monitor.

The engine never controls existing windows/tabs. No arbitrary executable,
shell, script, HWND or browser endpoint is accepted from an MCP caller.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid

from .core import PolicyError, reject_links
from .host_client import atomic_json, enabled
from .resources import LIMITS, Governor, WindowsSampler, require_capacity


class HostService:
    def __init__(self, agent):
        self.agent = agent
        self.folder = agent.state / "host"
        reject_links(self.folder)
        for name in ("inbox", "replies", "profiles"):
            (self.folder / name).mkdir(parents=True, exist_ok=True)
            reject_links(self.folder / name)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.maintenance = threading.Event()
        self.sessions = {}
        self.resource = dict(
            at=0, admission_ready=False, reason="warming_up", paused=True
        )
        self.computer = None
        self.sampler = WindowsSampler()
        self.governor = Governor()
        self.current = None
        # A command claimed by a dead worker has an unknown outcome, never replay it.
        for p in (self.folder / "inbox").iterdir():
            reject_links(p)
            if p.is_file():
                p.unlink()
        # Job objects kill orphaned owned children at GUI exit/crash. Profiles
        # belong solely to this private engine and contain no user profile data.
        for p in (self.folder / "profiles").iterdir():
            reject_links(p)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        self.monitor = threading.Thread(
            target=self._monitor, daemon=True, name="UDB resource monitor"
        )
        self.worker = threading.Thread(
            target=self._work, daemon=True, name="UDB host actions"
        )
        self.monitor.start()
        self.worker.start()

    def snapshot(self):
        sessions = []
        for sid, row in list(self.sessions.items()):
            obj = row["object"]
            usage = {}
            try:
                usage = obj.proc.usage() if obj.proc else {}
            except Exception:
                pass  # Resource metrics are optional; keep the session inventory available.
            sessions.append(
                dict(
                    session_id=sid,
                    kind=row["kind"],
                    task=row["task"],
                    expires=row["expires"],
                    tabs=len(obj.tabs) if row["kind"] == "browser" else 0,
                    pages_frozen=getattr(obj, "frozen", False),
                    freeze_errors=getattr(obj, "freeze_errors", 0),
                    **usage,
                )
            )
        return dict(
            worker_ready=not self.stop_event.is_set(),
            enabled=enabled(self.agent),
            resources=dict(self.resource),
            sessions=sessions,
            running_operation=self.current,
            host_mouse_keyboard=False,
            existing_user_windows=False,
            native_apps=["workspace"],
            browser_mode="separate_headless_profile",
        )

    def publish(self):
        atomic_json(self.folder / "status.json", self.snapshot())

    def _monitor(self):
        while not self.stop_event.is_set():
            try:
                self.resource = self.governor.update(self.sampler.sample())
                self.maintenance.set()
                if not enabled(self.agent):
                    # Revocation need not wait behind a slow CDP call. Close
                    # the captured private jobs first; ordinary cleanup follows.
                    for row in list(self.sessions.values()):
                        try:
                            row["object"].proc.close()
                        except Exception:
                            pass  # Revocation proceeds; the maintenance loop retries closure.
                self.publish()
            except Exception:
                self.resource = dict(
                    at=time.time(),
                    cpu_percent=None,
                    ram_percent=None,
                    available_mib=0,
                    admission_ready=False,
                    paused=True,
                    reason="monitor_error",
                )
                try:
                    self.publish()
                except Exception:
                    pass  # Status cannot be reported if status publication itself failed.
            self.stop_event.wait(2)

    def _maintain(self):
        for sid, row in list(self.sessions.items()):
            if (
                not enabled(self.agent)
                or time.time() > row["expires"]
                or not row["object"].proc.alive()
            ):
                self._close(sid)
            elif row["kind"] == "browser":
                row["object"].deadline = time.monotonic() + 4
                row["object"].freeze(not self.resource.get("admission_ready", False))

    def _close(self, sid):
        row = self.sessions.pop(sid, None)
        if row:
            row["object"].close()
            self.agent.audit("host_close", dict(session_id=sid, kind=row["kind"]))
        return dict(closed=bool(row), session_id=sid)

    def dispatch(self, op, args):
        if op == "computer":
            if self.computer is None:
                from .computer import Computer

                self.computer = Computer(self.agent)
            return self.computer.dispatch(
                args.get("mode", ""), args.get("arguments", {}), self.resource
            )
        if op == "stop_all":
            for sid in list(self.sessions):
                self._close(sid)
            return dict(stopped=True)
        sid = args.get("session_id")
        if op == "close":
            if not isinstance(sid, str) or not re.fullmatch("[a-f0-9]{32}", sid):
                raise PolicyError("Invalid session ID")
            return self._close(sid)
        if not enabled(self.agent):
            raise PolicyError("Host background mode disabled by owner")
        require_capacity(self.resource, 512 if op == "start" else 128)
        if op == "start":
            kind = args.get("kind")
            task = args.get("task")
            duration = args.get("duration_seconds", 900)
            if kind not in ("browser", "workspace"):
                raise PolicyError("Supported host apps: browser, workspace")
            if not isinstance(task, str) or not 1 <= len(task) <= 2000:
                raise PolicyError("Describe the authorized task")
            if not isinstance(duration, int) or not 60 <= duration <= 1800:
                raise PolicyError("Session duration: 60..1800 seconds")
            if len(self.sessions) >= LIMITS["max_sessions"]:
                raise PolicyError("Maximum 2 owned sessions; close a session first")
            if any(row["kind"] == kind for row in self.sessions.values()):
                raise PolicyError("Reuse the existing " + kind + " session")
            sid = uuid.uuid4().hex
            if kind == "browser":
                from .host_browser import Browser

                obj = Browser(self.folder / "profiles" / sid)
            else:
                from .host_workspace import Workspace

                obj = Workspace(self.agent.home, sid)
            self.sessions[sid] = dict(
                kind=kind, task=task, expires=time.time() + duration, object=obj
            )
            self.agent.audit("host_start", dict(session_id=sid, kind=kind))
            return dict(
                session_id=sid,
                kind=kind,
                no_user_input=True,
                tabs=obj.listing() if kind == "browser" else [],
                expires=self.sessions[sid]["expires"],
            )
        if not isinstance(sid, str) or sid not in self.sessions:
            raise PolicyError("Unknown or expired owned host session")
        row = self.sessions[sid]
        if time.time() > row["expires"]:
            self._close(sid)
            raise PolicyError("Host session expired")
        obj = row["object"]
        action = args.get("action")
        if op == "browser" and row["kind"] == "browser":
            obj.deadline = time.monotonic() + 20
            if action == "list":
                return dict(tabs=obj.listing())
            if action == "new":
                return obj.new(args.get("url", "about:blank"))
            tab = args.get("tab_id", "")
            if action == "navigate":
                return obj.navigate(tab, args.get("url", ""))
            if action == "read":
                return obj.read(tab)
            if action in ("fill", "click"):
                return obj.element(
                    tab, action, args.get("selector", ""), args.get("value", "")
                )
            if action == "screenshot":
                return dict(png_base64=obj.screenshot(tab))
            if action == "close_tab":
                return obj.close_tab(tab)
            raise PolicyError("Unsupported browser action")
        if op == "workspace" and row["kind"] == "workspace":
            if action == "read":
                return obj.read()
            if action == "set_text":
                return obj.set_text(args.get("text", ""))
        raise PolicyError("Unsupported action for this owned session")

    def _work(self):
        while not self.stop_event.is_set():
            try:
                if self.maintenance.is_set():
                    with self.lock:
                        self.maintenance.clear()
                        self._maintain()
                for p in sorted((self.folder / "inbox").glob("*.json")):
                    if self.stop_event.is_set():
                        break
                    reject_links(p)
                    if p.stat().st_size > 16 * 1048576:
                        p.unlink()
                        continue
                    claimed = p.with_suffix(".working")
                    p.replace(claimed)
                    try:
                        data = json.loads(claimed.read_text("utf-8"))
                        rid = data["id"]
                        if not re.fullmatch("[a-f0-9]{32}", rid) or claimed.stem != rid:
                            raise ValueError("invalid command identity")
                        if not time.time() < data["expires"] <= time.time() + 30:
                            raise PolicyError("Host command expired; not executed")
                        with self.lock:
                            self.current = data["op"]
                            try:
                                self.publish()
                                result = self.dispatch(data["op"], data.get("args", {}))
                            finally:
                                self.current = None
                        reply = dict(ok=True, result=result)
                    except Exception as exc:
                        reply = dict(
                            ok=False,
                            error=str(exc)[:500]
                            if isinstance(exc, (PolicyError, ValueError))
                            else "Owned host operation failed; inspect status before retrying",
                        )
                    finally:
                        claimed.unlink(missing_ok=True)
                    if re.fullmatch("[a-f0-9]{32}", p.stem):
                        atomic_json(self.folder / "replies" / (p.stem + ".json"), reply)
                    self.publish()
                for p in (self.folder / "replies").glob("*.json"):
                    if time.time() - p.stat().st_mtime > 120:
                        p.unlink(missing_ok=True)
            except Exception:
                pass  # Transient mailbox errors are retried on the next poll.
            self.stop_event.wait(0.06)

    def close(self):
        self.stop_event.set()
        # Let a bounded in-flight operation finish, then close only our jobs.
        self.worker.join(timeout=30)
        self.monitor.join(timeout=8)
        with self.lock:
            for sid in list(self.sessions):
                self._close(sid)
            if self.computer is not None:
                self.computer.close()
        self.publish()
