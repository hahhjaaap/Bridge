"""UDB one-command companion v4.1; stdlib only, requires the dev.6 EXE for udb test.

Local readiness is not proof that a ChatGPT session has refreshed its connection.
Owner opt-in is delegated to an attended console in the EXE, never an MCP setter.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

VERSION = "4.1.1"


class CommandError(Exception):
    pass


def regular(path: Path) -> Path:
    """Reject symlinks and Windows reparse points along a selected path."""
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and
                getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise CommandError("Linked installation/state paths are not supported.")
    return path


def read_text(path: Path, limit: int) -> str:
    regular(path)
    # Bound the read itself, including files changed between stat and open.
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CommandError("A local state file exceeds its size limit.")
    return data.decode("utf-8-sig")


def read_object(path: Path, limit: int = 131072) -> dict:
    value = json.loads(read_text(path, limit))
    if not isinstance(value, dict):
        raise CommandError("Invalid local state format.")
    return value


def executable(home: Path) -> Path:
    regular(home)
    metadata = home / "installation.json"
    if metadata.exists():
        info = read_object(metadata)
        active = info.get("active", "")
        digest = info.get("sha256", "")
        if (not isinstance(active, str) or
                not re.fullmatch(r"[0-9A-Za-z.-]{1,90}", active) or active in (".", "..") or
                not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
            raise CommandError("Invalid installed release metadata.")
        target = regular(home / "releases" / active / "UniversalDesktopBridge.exe")
        if not target.is_file():
            raise CommandError("The installed application is missing.")
        sha = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(block)
        if sha.hexdigest() != digest.lower():
            raise CommandError("Installed EXE integrity check failed.")
        return target
    target = regular(home / "UniversalDesktopBridge.exe")
    if not target.is_file():
        raise CommandError("Application not found. Select the existing UDB installation.")
    return target


def process_image(pid: int) -> str | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel.CloseHandle(handle)
    return None


def manager_running(home: Path, exe: Path, image=process_image) -> bool:
    try:
        first = read_text(home / "state" / "manager.lock", 4096).splitlines()[0]
        if not re.fullmatch(r"[0-9]{1,10}", first):
            return False
        pid = int(first)
        if not 0 < pid <= 0xFFFFFFFF:
            return False
        actual = image(pid)
        if not actual:return False
        normalized=lambda p:os.path.normcase(os.path.abspath(p))
        if normalized(actual)==normalized(exe):return True
        # Live upgrades retain mapped images until the attended manager exits.
        # Only a sibling image explicitly recorded by the updater AND still
        # matching its SHA256 can satisfy the existing lock/PID check.
        candidate=Path(actual)
        if normalized(candidate.parent)!=normalized(home):return False
        if not re.fullmatch(r'UniversalDesktopBridge\.[A-Za-z0-9-]+\.running\.exe',candidate.name):return False
        hashes=read_object(home/'UDB-Command'/'running-images.json',4096)
        expected=hashes.get(candidate.name)
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):return False
        candidate=regular(candidate)
        if not candidate.is_file() or candidate.stat().st_size>128*1024*1024:return False
        digest=hashlib.sha256()
        with candidate.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        return digest.hexdigest()==expected
    except (OSError, ValueError, IndexError, CommandError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tunnel_ready(home: Path, opener=None) -> bool:
    try:
        base = read_text(home / "state" / "tunnel-health.url", 128).strip()
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", base)
        if not match or not 1 <= int(match[1]) <= 65535:
            return False
        opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(base + "/readyz", timeout=0.6) as reply:
            return reply.status == 200
    except (OSError, ValueError, CommandError):
        return False


def background_state(home: Path, now=None) -> str:
    try:
        prefs = read_object(home / "state" / "host-settings.json")
    except FileNotFoundError:
        return "disabled"
    except (OSError, ValueError, CommandError):
        return "invalid_settings"
    if prefs.get("enabled") is not True:
        return "disabled"
    try:
        state = read_object(home / "state" / "host" / "status.json")
        stamp = state.get("resources", {}).get("at")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return "unavailable"
        age = (time.time() if now is None else now) - stamp
        if not -2 <= age <= 6 or state.get("worker_ready") is not True:
            return "unavailable"
        return "paused" if state["resources"].get("paused") is True else "ready"
    except (OSError, ValueError, TypeError, AttributeError, CommandError):
        return "unavailable"


def headless_state(home: Path) -> dict | None:
    try:
        data = read_object(home / "state" / "headless-status.json")
        if (data.get("mode") != "headless" or data.get("phase") != "running" or
                not isinstance(data.get("instance"), str) or
                not re.fullmatch(r"[a-f0-9]{32}", data["instance"])):
            return None
        age = time.time() - data["at"]
        pid = read_text(home / "state" / "manager.lock", 4096).splitlines()[0]
        if not -2 <= age <= 6 or str(data.get("pid")) != pid:
            return None
        return data
    except (OSError, ValueError, TypeError, IndexError, KeyError, CommandError):
        return None


def inspect(home: Path, exe: Path, running=manager_running, ready=tunnel_ready) -> dict:
    active = running(home, exe)
    connected = active and ready(home)
    hidden = headless_state(home) if active else None
    return dict(companion_version=VERSION, manager_running=active,
                manager_mode=("headless" if hidden else "legacy_or_unknown" if active else "stopped"),
                local_tunnel_ready=connected, background=background_state(home),
                chatgpt_connection_verified=False,
                next_action=("Call bridge_status in ChatGPT to verify the session." if connected else
                             "The background agent is retrying. Run udb doctor to inspect local readiness." if hidden else
                             "Close the legacy UDB window once, then run udb to use headless mode." if active else
                             "Run udb to open the existing installation."))


def doctor(home: Path, report: dict) -> list[dict]:
    checks = []
    try:
        cfg = read_object(home / "state" / "connection.json")
        valid = isinstance(cfg.get("tunnel_id"), str) and bool(
            re.fullmatch(r"tunnel_[a-z0-9]{32}", cfg["tunnel_id"]))
    except (OSError, ValueError, CommandError):
        valid = False
    checks.append(dict(check="connection_configuration", ok=valid,
                       action="Already configured." if valid else
                       "Open connection settings in UDB. Keep runtime keys out of chat."))
    checks.append(dict(check="manager", ok=report["manager_running"],
                       action="Running." if report["manager_running"] else "Run udb."))
    checks.append(dict(check="local_tunnel", ok=report["local_tunnel_ready"],
                       action=report["next_action"]))
    checks.append(dict(check="chatgpt_session", ok=None,
                       action="A successful bridge_status call is required; local checks cannot verify ChatGPT."))
    return checks


@contextlib.contextmanager
def launch_gate(home: Path, timeout=40):
    # Per-user temporary directory and installation; no state/grant files changed.
    identity = str(home).casefold() if os.name == "nt" else str(home)
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    root = Path(tempfile.gettempdir()) / ("udb-command-" + (
        str(os.getuid()) if hasattr(os, "getuid") else "user"))
    root.mkdir(mode=0o700, exist_ok=True)
    path = regular(root / (token + ".lock"))
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        until = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= until:
                    raise CommandError("Another udb startup is still running. Try again shortly.") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def start(home: Path, exe: Path, wait=30, snapshot=inspect, spawn=subprocess.Popen,
          clock=time.monotonic, pause=time.sleep) -> dict:
    with launch_gate(home):
        report = snapshot(home, exe)
        if not report["manager_running"]:
            spawn([str(exe), "--home", str(home), "--headless"], cwd=str(home),
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False)
        deadline = clock() + wait
        while True:
            report = snapshot(home, exe)
            if report["local_tunnel_ready"] or clock() >= deadline:
                return report
            pause(min(0.3, max(0, deadline - clock())))


def stop(home: Path, exe: Path, wait=30, snapshot=inspect, clock=time.monotonic, pause=time.sleep):
    with launch_gate(home):
        report = snapshot(home, exe)
        if not report["manager_running"]:
            return report
        state = headless_state(home)
        if not state:
            raise CommandError("Only the headless agent can be stopped with udb stop. Close the legacy UDB window once.")
        target = regular(home / "state" / "headless-stop.json")
        tmp = regular(target.with_name(target.name + "." + os.urandom(8).hex() + ".tmp"))
        try:
            with tmp.open("x", encoding="utf-8") as stream:
                json.dump(dict(instance=state["instance"], expires=time.time() + 45), stream)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        deadline = clock() + wait
        while True:
            report = snapshot(home, exe)
            if not report["manager_running"] or clock() >= deadline:
                return report
            pause(min(0.3, max(0, deadline - clock())))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open UDB or inspect local readiness without changing permissions.")
    parser.add_argument("action", nargs="?", default="start", choices=("start", "stop", "status", "doctor", "test", "revoke-access"))
    parser.add_argument("--access", choices=("computer",), help="Attended local-console opt-in, then connect")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--wait", type=int, default=30, choices=range(0, 61), metavar="0..60")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        regular(args.home.absolute())
        home = args.home.resolve()
        exe = executable(home)
        if args.access:
            if args.action!='start' or args.as_json:
                raise CommandError('Use udb --access computer in your own interactive terminal.')
            result=subprocess.run([str(exe),'--owner-computer-access','--home',str(home)],shell=False)
            if result.returncode:return result.returncode
        if args.action=='revoke-access':
            return subprocess.run([str(exe),'--revoke-computer-access','--home',str(home)],shell=False).returncode
        if args.action=='test':
            if os.name!='nt':raise CommandError('The installed-agent test requires Windows.')
            report=start(home,exe,args.wait)
            if not report['manager_running']:raise CommandError('Run udb before testing the installed agent.')
            return subprocess.run([str(exe),'--probe','--home',str(home)],shell=False).returncode
        if os.name != "nt" and args.action == "start":
            raise CommandError("Starting the Windows application requires Windows.")
        if args.action == "start":
            report = start(home, exe, args.wait)
        elif args.action == "stop":
            report = stop(home, exe, args.wait)
        else:
            report = inspect(home, exe)
        if args.action == "doctor":
            report["checks"] = doctor(home, report)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=True))
        else:
            print("UDB: local tunnel " + ("ready" if report["local_tunnel_ready"] else "not ready") +
                  "; background " + report["background"] + "; mode " + report["manager_mode"] + ".")
            print(report["next_action"])
            for check in report.get("checks", []):
                mark = "OK" if check["ok"] is True else "CHECK" if check["ok"] is False else "UNVERIFIED"
                print(f"{mark} {check['check']}: {check['action']}")
        healthy = (not report["manager_running"] if args.action == "stop" else
                   report["local_tunnel_ready"] and all(c["ok"] is not False for c in report.get("checks", [])))
        return 0 if healthy else 2
    except (OSError, ValueError, CommandError) as exc:
        # Do not echo raw state contents, credential errors, or process output.
        message = (str(exc) if isinstance(exc, CommandError) else
                   "Cannot inspect/start this installation. Check its path, integrity and local state.")
        if args.as_json:
            print(json.dumps(dict(error=message, chatgpt_connection_verified=False)))
        else:
            print("UDB: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
