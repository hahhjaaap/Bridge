"""GUI-owned host engine: one serial action worker and a 2-second monitor.

The engine never controls existing windows/tabs. No arbitrary executable,
shell, script, HWND or browser endpoint is accepted from an MCP caller.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid

from .browser_actions import dispatch_browser
from .core import PolicyError, reject_links
from .host_client import _read_json, atomic_json, enabled
from .resources import LIMITS, Governor, WindowsSampler, require_capacity

_IDLE_POLL_SECONDS = 0.25
_REPLY_CLEANUP_SECONDS = 5.0


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
        from .desktop_control import PauseHotkey

        self.pause_hotkey = PauseHotkey(agent)
        atomic_json(agent.state / "browser-handoff.json", {"active": False})
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
                    presentation="visible"
                    if getattr(obj, "visible", False)
                    else "background",
                    pages_frozen=getattr(obj, "frozen", False),
                    freeze_errors=getattr(obj, "freeze_errors", 0),
                    closing=bool(row.get("closing")),
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
            emergency_hotkey_available=bool(
                getattr(getattr(self, "pause_hotkey", None), "available", False)
            ),
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
            try:
                obj = row["object"]
                if (
                    row.get("closing")
                    or not enabled(self.agent)
                    or time.time() > row["expires"]
                    or obj.proc is None
                    or not obj.proc.alive()
                ):
                    self._close(sid)
                elif row["kind"] == "browser" and not getattr(obj, "visible", False):
                    obj.deadline = time.monotonic() + 4
                    obj.freeze(not self.resource.get("admission_ready", False))
            except Exception:
                continue  # Keep failed cleanup tracked and maintain the other sessions.

    def _close(self, sid):
        row = self.sessions.get(sid)
        if row:
            row["closing"] = True
            row["object"].close()
            if row["kind"] == "browser":
                atomic_json(
                    self.agent.state / "browser-handoff.json", {"active": False}
                )
            self.agent.audit("host_close", dict(session_id=sid, kind=row["kind"]))
            self.sessions.pop(sid, None)
        return dict(closed=bool(row), session_id=sid)

    def _close_all(self):
        failures = []
        for sid in list(self.sessions):
            try:
                self._close(sid)
            except Exception:
                failures.append(sid)
        return failures

    def _sync_handoff(self, browser):
        active = bool(getattr(browser, "visible", False))
        if active:
            try:
                active = bool(browser.proc and browser.proc.alive())
            except Exception:
                active = True  # Uncertain window ownership keeps input paused.
        atomic_json(self.agent.state / "browser-handoff.json", {"active": active})

    def _session(self, sid, *, kind=None):
        """Resolve an owned session once and expire it before any action."""
        row = self.sessions.get(sid) if isinstance(sid, str) else None
        if row is None or (kind is not None and row["kind"] != kind):
            message = (
                "Unknown owned browser session"
                if kind == "browser"
                else "Unknown or expired owned host session"
            )
            raise PolicyError(message)
        if row.get("closing"):
            raise PolicyError("Owned session is closing; inspect host status")
        if time.time() > row["expires"]:
            self._close(sid)
            raise PolicyError("Host session expired")
        return row

    def _start_session(self, args):
        """Allocate after dispatch has checked owner enablement and capacity."""
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
            from .browser_settings import remember_login
            from .host_browser import Browser

            if remember_login(self.agent):
                obj = Browser(self.folder / "browser-profile", persistent=True)
            else:
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

    def _open_browser(self, args):
        sid = args.get("session_id")
        from .host_browser import validate_url

        open_deadline = time.monotonic() + 20
        url = validate_url(args.get("url", "about:blank"))
        if sid:
            self._session(sid, kind="browser")
        else:
            for existing_sid, row in list(self.sessions.items()):
                if row["kind"] != "browser":
                    continue
                if time.time() > row["expires"]:
                    self._close(existing_sid)
                else:
                    sid = existing_sid
                    break
            if not sid:
                started = self._start_session(
                    dict(kind="browser", task="Open a UDB-owned background web page")
                )
                sid = started["session_id"]
        # Never replay a navigation or fall back to a visible user browser.
        obj = self.sessions[sid]["object"]
        obj.deadline = open_deadline
        if getattr(obj, "visible", False):
            raise PolicyError(
                "The owner has browser control; return it to background in the local UDB manager"
            )
        result = obj.new(url)
        return dict(
            result,
            session_id=sid,
            browser_mode="separate_headless_profile",
            owner="UDB",
            no_user_input=True,
            identity="UDB • AI",
        )

    def dispatch(self, op, args):
        if op == "computer":
            if self.computer is None:
                from .computer import Computer

                self.computer = Computer(self.agent)
            return self.computer.dispatch(
                args.get("mode", ""), args.get("arguments", {}), self.resource
            )
        if op == "stop_all":
            failed = self._close_all()
            if failed:
                raise PolicyError(
                    "Owned session cleanup pending; inspect status: "
                    + ", ".join(failed)
                )
            return dict(stopped=True)
        if op == "close":
            sid = args.get("session_id")
            if not isinstance(sid, str) or not re.fullmatch("[a-f0-9]{32}", sid):
                raise PolicyError("Invalid session ID")
            return self._close(sid)
        if not enabled(self.agent):
            raise PolicyError("Host background mode disabled by owner")
        if op == "browser_presentation":
            # Local manager operation; this op has no MCP registration.
            visible = args.get("visible")
            if type(visible) is not bool:
                raise PolicyError("Invalid browser presentation")
            require_capacity(self.resource, 512)
            sid = next(
                (key for key, row in self.sessions.items() if row["kind"] == "browser"),
                None,
            )
            if sid is None:
                sid = self._start_session(
                    dict(kind="browser", task="Local owner browser login")
                )["session_id"]
            obj = self._session(sid, kind="browser")["object"]
            obj.deadline = time.monotonic() + 20
            if visible:
                self.sessions[sid]["expires"] = max(
                    self.sessions[sid]["expires"], time.time() + 900
                )
                atomic_json(self.agent.state / "browser-handoff.json", {"active": True})
            try:
                result = obj.switch_presentation(visible)
            finally:
                self._sync_handoff(obj)
            return dict(result, session_id=sid)
        opening_browser = op == "browser" and args.get("action") == "open"
        require_capacity(
            self.resource, 512 if op == "start" or opening_browser else 128
        )
        if opening_browser:
            return self._open_browser(args)
        if op == "start":
            return self._start_session(args)
        row = self._session(args.get("session_id"))
        obj = row["object"]
        action = args.get("action")
        if op == "browser" and row["kind"] == "browser":
            obj.deadline = time.monotonic() + 20
            if getattr(obj, "visible", False):
                if action == "list":
                    return dict(tabs=[], presentation="visible", owner_control=True)
                raise PolicyError(
                    "The owner has browser control; return it to background in the local UDB manager"
                )
            if action in {"upload", "download"}:
                return self._browser_transfer(obj, args)
            return dispatch_browser(obj, args)
        if op == "workspace" and row["kind"] == "workspace":
            if action == "read":
                return obj.read()
            if action == "set_text":
                return obj.set_text(args.get("text", ""))
        raise PolicyError("Unsupported action for this owned session")

    def _browser_transfer(self, browser, args):
        import hashlib

        from . import browser_files, computer_access
        from .computer_files import Files

        computer_access.require(self.agent)
        files = Files(self.agent)
        tab, expected_origin = args.get("tab_id", ""), args.get("expected_origin", "")
        if args["action"] == "upload":
            source = files.path(args.get("path", ""))
            data = files.data(source)
            digest = hashlib.sha256(data).hexdigest()
            if args.get("expected_sha256") != digest:
                raise PolicyError(
                    "Read the source SHA256 before uploading; file changed or expected_sha256 is missing"
                )
            return browser_files.upload(
                browser, tab, args.get("selector", ""), source, data, expected_origin
            )
        target = files.path(args.get("destination", ""))
        files.check_expected(target, "NEW")
        data, content_type = browser_files.download(
            browser, tab, args.get("url", ""), expected_origin
        )
        result = files.write_bytes(str(target), data, "NEW")
        return dict(result, content_type=content_type, executed=False)

    def _prune_replies(self):
        cutoff = time.time() - 120
        for p in (self.folder / "replies").glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                continue  # A client may consume a reply during the scan.

    def _process_command(self, path):
        reject_links(path)
        if not re.fullmatch("[a-f0-9]{32}", path.stem) or not path.is_file():
            return False
        claimed = path.with_suffix(".working")
        reject_links(claimed)
        path.replace(claimed)
        try:
            try:
                data = _read_json(claimed, 16 * 1048576)
                if not isinstance(data, dict) or data.get("id") != claimed.stem:
                    raise ValueError("invalid command identity")
                expires = data.get("expires")
                if (
                    type(expires) not in (int, float)
                    or not time.time() < expires <= time.time() + 30
                ):
                    raise PolicyError("Host command expired; not executed")
                op, args = data.get("op"), data.get("args", {})
                if not isinstance(op, str) or not isinstance(args, dict):
                    raise ValueError("Invalid host command envelope")
                with self.lock:
                    self.current = op
                    try:
                        self.publish()
                        result = self.dispatch(op, args)
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
            atomic_json(self.folder / "replies" / path.name, reply)
        finally:
            claimed.unlink(missing_ok=True)
        self.publish()
        return True

    def _work(self):
        poll = 0.06
        next_cleanup = 0.0
        while not self.stop_event.is_set():
            processed = False
            try:
                if self.maintenance.is_set():
                    with self.lock:
                        self.maintenance.clear()
                        self._maintain()
                for p in sorted((self.folder / "inbox").glob("*.json")):
                    if self.stop_event.is_set():
                        break
                    try:
                        processed = self._process_command(p) or processed
                    except Exception:
                        continue  # A bad/locked entry must not block the rest of the inbox.
                now = time.monotonic()
                if now >= next_cleanup:
                    next_cleanup = now + _REPLY_CLEANUP_SECONDS
                    self._prune_replies()
            except Exception:
                pass  # Transient mailbox errors are retried on the next poll.
            poll = 0.06 if processed else min(_IDLE_POLL_SECONDS, poll * 1.5)
            self.stop_event.wait(poll)

    def close(self):
        self.stop_event.set()
        failures = []
        if getattr(self, "pause_hotkey", None):
            try:
                self.pause_hotkey.close()
            except Exception:
                failures.append("hotkey")
        # Let a bounded in-flight operation finish, then close only our jobs.
        self.worker.join(timeout=30)
        self.monitor.join(timeout=8)
        with self.lock:
            failures.extend(self._close_all())
            if self.computer is not None:
                try:
                    self.computer.close()
                except Exception:
                    failures.append("computer jobs")
        try:
            self.publish()
        except Exception:
            failures.append("status publication")
        if failures:
            raise PolicyError(
                "Host cleanup incomplete; inspect status: " + ", ".join(failures)
            )
