"""Owner CLI lifecycle implementation behind the public udb command.

Local readiness is not proof that a ChatGPT session has refreshed its connection.
Owner opt-in is delegated to an attended console in the EXE, never an MCP setter.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from ._version import VERSION
from .config import manager_options
from .connection import is_tunnel_id
from .launcher import worker_command
from .owner_cli import parse_args, render_report, report_healthy


class CommandError(Exception):
    pass


def regular(path: Path) -> Path:
    """Reject symlinks and Windows reparse points along a selected path."""
    for item in (path, *path.parents):
        if item.is_symlink() or (
            item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400
        ):
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
    if getattr(sys, "frozen", False):
        # The standalone manager is embedded in this executable. Legacy
        # installation metadata must not make its lifecycle depend on an old EXE.
        return regular(Path(sys.executable))
    metadata = home / "installation.json"
    if metadata.exists():
        info = read_object(metadata)
        active = info.get("active", "")
        digest = info.get("sha256", "")
        if (
            not isinstance(active, str)
            or not re.fullmatch(r"[0-9A-Za-z.-]{1,90}", active)
            or active in (".", "..")
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
        ):
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
        return Path(sys.executable)
    return target


def process_image(pid: int) -> str | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
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
        if not actual:
            return False

        def normalized(path: str | Path) -> str:
            return os.path.normcase(os.path.abspath(path))

        # Windows virtual environments can redirect to the base interpreter.
        # An interpreter image alone never identifies an owned manager.
        interpreters = {
            normalized(sys.executable),
            normalized(getattr(sys, "_base_executable", sys.executable)),
        }
        if normalized(actual) in interpreters:
            return headless_state(home) is not None
        if normalized(actual) == normalized(exe):
            return True
        # Live upgrades retain mapped images until the attended manager exits.
        # Only a sibling image explicitly recorded by the updater AND still
        # matching its SHA256 can satisfy the existing lock/PID check.
        candidate = Path(actual)
        if normalized(candidate.parent) != normalized(home):
            return False
        if not re.fullmatch(
            r"UniversalDesktopBridge\.[A-Za-z0-9-]+\.running\.exe", candidate.name
        ):
            return False
        hashes = read_object(home / "UDB-Command" / "running-images.json", 4096)
        expected = hashes.get(candidate.name)
        if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
            return False
        candidate = regular(candidate)
        if not candidate.is_file() or candidate.stat().st_size > 128 * 1024 * 1024:
            return False
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == expected
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
            urllib.request.ProxyHandler({}), NoRedirect()
        )
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
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
        ):
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
        if (
            data.get("mode") != "headless"
            or data.get("phase") != "running"
            or not isinstance(data.get("instance"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", data["instance"])
        ):
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
    count = (hidden or {}).get("recovery_count", 0)
    count = count if type(count) is int and 0 <= count <= 10**9 else 0
    reason = (hidden or {}).get("last_recovery", "")
    reason = reason if reason in ("", "tunnel_unready") else ""
    return dict(
        companion_version=VERSION,
        manager_running=active,
        manager_mode=(
            "headless" if hidden else "legacy_or_unknown" if active else "stopped"
        ),
        local_tunnel_ready=connected,
        background=background_state(home),
        connection_mode=hidden.get("connection_mode", "online")
        if hidden
        else "unknown",
        chatgpt_connection_verified=False,
        tunnel_recovery={
            "count": count,
            "last_reason": reason,
            "replays_tool_calls": False,
        },
        next_action=(
            "Call bridge_status in ChatGPT to verify the session."
            if connected
            else "The local agent is running offline. Run udb setup to connect."
            if hidden and hidden.get("connection_mode") == "offline"
            else "The background agent is retrying. Run udb doctor to inspect local readiness."
            if hidden
            else "Close the legacy UDB window once, then run udb to use headless mode."
            if active
            else "Run udb to open the existing installation."
        ),
    )


def remote_session_action(report: dict, *, offline: bool) -> str:
    """Guide remote troubleshooting without claiming to observe the ChatGPT UI."""
    if offline:
        return (
            "Local/offline mode: a ChatGPT remote session is not expected. "
            "Use udb setup on the original computer to enable online mode."
        )
    if not report.get("local_tunnel_ready", False):
        return (
            "The local tunnel is not ready. Run udb status --json and "
            "udb doctor --json on the original computer; resolve local "
            "connection problems before retrying tools in ChatGPT."
        )
    return (
        "Local tunnel is ready, but ChatGPT tool access is NOT verified. "
        "From the other device call bridge_status. If the plugin shows "
        "Connected yet calls fail with 'Session terminated', reconnect or "
        "refresh the ChatGPT plugin/session and verify bridge_status again. "
        "Do not replay a command whose outcome is uncertain."
    )


def doctor(home: Path, report: dict) -> list[dict]:
    from .config import effective
    from .onboarding import saved_mode

    checks = []
    settings = effective(home)
    try:
        mode = saved_mode(settings)
        mode_valid = True
    except (OSError, ValueError, CommandError):
        mode = None
        mode_valid = False
    offline = report.get("connection_mode") == "offline" or (
        not report["manager_running"] and mode == "offline"
    )
    checks.append(
        dict(
            check="startup_mode",
            ok=mode_valid,
            action="Saved startup choice is valid."
            if mode_valid
            else "Run udb setup to select a valid startup mode.",
        )
    )
    try:
        cfg = read_object(home / "state" / "connection.json")
        valid = is_tunnel_id(cfg.get("tunnel_id"))
    except (OSError, ValueError, CommandError):
        valid = False
    checks.append(
        dict(
            check="connection_configuration",
            ok=None if offline else valid,
            action="Already configured."
            if valid
            else "Not required in local mode."
            if offline
            else "Run udb setup --edit locally. Keep runtime keys out of chat.",
        )
    )
    checks.append(
        dict(
            check="manager",
            ok=report["manager_running"],
            action="Running." if report["manager_running"] else "Run udb.",
        )
    )
    checks.append(
        dict(
            check="local_tunnel",
            ok=None if offline else report["local_tunnel_ready"],
            action="Not required in local mode." if offline else report["next_action"],
        )
    )
    from .components import runtime_report

    try:
        runtime = runtime_report()
        helpers = {row["name"] for row in runtime["components"]}
        bundled = "tunnel-client" in helpers
        if not bundled:
            from .components import component_path

            bundled = component_path("tunnel-client", home) is not None
        checks.append(
            dict(
                check="runtime_integrity", ok=True, action="Runtime inventory verified."
            )
        )
        checks.append(
            dict(
                check="tunnel_component",
                ok=None if offline else bundled,
                action="Available."
                if bundled
                else "Install the Windows standalone release to include tunnel-client.",
            )
        )
        checks.append(
            dict(
                check="office_modules",
                ok=all(runtime["office_modules"].values()),
                action="Install the complete standalone release for document tools."
                if not all(runtime["office_modules"].values())
                else "Document modules available.",
            )
        )
    except (OSError, ValueError, TypeError, KeyError):
        checks.append(
            dict(
                check="runtime_integrity",
                ok=False,
                action="Runtime integrity failed. Reinstall the official standalone release.",
            )
        )
    try:
        from .autostart import status

        startup = status(settings)
        checks.append(
            dict(
                check="autostart",
                ok=False
                if startup["enabled"] and not startup["matches_current"]
                else None,
                action="Startup entry needs repair: run udb autostart enable."
                if startup["enabled"] and not startup["matches_current"]
                else "Enabled for this Windows user."
                if startup["enabled"]
                else "Optional: udb autostart enable; undo with udb autostart disable.",
            )
        )
        from .maintenance import installation

        managed = installation()
        checks.append(
            dict(
                check="installation",
                ok=True if managed else None,
                action="Managed installation: udb update --check; udb rollback restores the retained previous release."
                if managed
                else "Portable/source installation. The one-command installer enables update and rollback.",
            )
        )
    except (OSError, ValueError, CommandError):
        checks.append(
            dict(
                check="installation",
                ok=False,
                action="Installation metadata is invalid. Re-run the official installer.",
            )
        )
    state = headless_state(home)
    if report["manager_running"]:
        checks.append(
            dict(
                check="agent_heartbeat",
                ok=state is not None,
                action="Headless agent responds."
                if state
                else "Agent heartbeat is stale. Run udb status; close a legacy window before restarting.",
            )
        )
    checks.append(
        dict(
            check="credentials",
            ok=None,
            action="Keys are not read by doctor. If authentication fails, use udb setup --edit in your own terminal.",
        )
    )
    checks.append(
        dict(
            check="chatgpt_session",
            ok=None,
            action=remote_session_action(report, offline=offline),
        )
    )
    return checks


@contextlib.contextmanager
def launch_gate(home: Path, timeout=40):
    # Per-user temporary directory and installation; no state/grant files changed.
    identity = str(home).casefold() if os.name == "nt" else str(home)
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    root = Path(tempfile.gettempdir()) / (
        "udb-command-" + (str(os.getuid()) if hasattr(os, "getuid") else "user")
    )
    root.mkdir(mode=0o700, exist_ok=True)
    path = regular(root / (token + ".lock"))
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
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
                    raise CommandError(
                        "Another udb startup is still running. Try again shortly."
                    ) from None
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


def start(
    home: Path,
    exe: Path,
    wait=30,
    snapshot=inspect,
    spawn=subprocess.Popen,
    clock=time.monotonic,
    pause=time.sleep,
    connect=True,
) -> dict:
    with launch_gate(home):
        report = snapshot(home, exe)
        if report["manager_running"] and (
            (connect and report.get("connection_mode") == "offline")
            or (not connect and report.get("connection_mode") == "online")
        ):
            raise CommandError(
                "The agent is already running in another connection mode. Run udb stop first."
            )
        if not report["manager_running"]:
            from .config import effective, prepare_workspace

            prepare_workspace(effective(home))
            child_env = os.environ.copy()
            if getattr(sys, "frozen", False):
                # The manager outlives this short-lived onefile CLI. It needs
                # its own extraction directory, owned by its own bootloader.
                child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            spawn(
                worker_command(
                    "core",
                    [
                        "--home",
                        str(home),
                        "--headless",
                        *manager_options(home),
                        *([] if connect else ["--no-connect"]),
                    ],
                ),
                cwd=str(home),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                shell=False,
            )
        deadline = clock() + wait
        while True:
            report = snapshot(home, exe)
            if (
                report["local_tunnel_ready"]
                or (
                    not connect
                    and report["manager_running"]
                    and report.get("connection_mode") == "offline"
                )
                or clock() >= deadline
            ):
                return report
            pause(min(0.3, max(0, deadline - clock())))


def stop(
    home: Path,
    exe: Path,
    wait=30,
    snapshot=inspect,
    clock=time.monotonic,
    pause=time.sleep,
):
    with launch_gate(home):
        report = snapshot(home, exe)
        if not report["manager_running"]:
            return report
        state = headless_state(home)
        if not state:
            raise CommandError(
                "Only the headless agent can be stopped with udb stop. Close the legacy UDB window once."
            )
        target = regular(home / "state" / "headless-stop.json")
        tmp = regular(
            target.with_name(target.name + "." + os.urandom(8).hex() + ".tmp")
        )
        try:
            with tmp.open("x", encoding="utf-8") as stream:
                json.dump(
                    dict(instance=state["instance"], expires=time.time() + 45), stream
                )
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
    args = parse_args(argv)
    try:
        regular(args.home.absolute())
        home = args.home.resolve()
        if (args.follow or args.detach) and args.action != "start":
            raise CommandError("--detach/--follow apply only to start.")
        if args.follow and args.as_json:
            raise CommandError("--follow cannot be combined with --json.")
        exe = executable(home)
        if args.offline and args.action != "start":
            raise CommandError("--offline applies only to start.")
        if args.access:
            if args.action != "start" or args.as_json:
                raise CommandError(
                    "Use udb --access computer in your own interactive terminal."
                )
            result = subprocess.run(
                worker_command(
                    "core", ["--owner-computer-access", "--home", str(home)]
                ),
                shell=False,
            )
            if result.returncode:
                return result.returncode
        if args.action == "revoke-access":
            return subprocess.run(
                worker_command(
                    "core", ["--revoke-computer-access", "--home", str(home)]
                ),
                shell=False,
            ).returncode
        if args.action == "test":
            if os.name != "nt":
                raise CommandError("The installed-agent test requires Windows.")
            report = start(home, exe, args.wait)
            if not report["manager_running"]:
                raise CommandError("Run udb before testing the installed agent.")
            return subprocess.run(
                worker_command("core", ["--probe", "--home", str(home)]), shell=False
            ).returncode
        if os.name != "nt" and args.action == "start":
            raise CommandError("Starting the Windows application requires Windows.")
        if args.action == "start":
            report = start(home, exe, args.wait, connect=not args.offline)
        elif args.action == "stop":
            report = stop(home, exe, args.wait)
        else:
            report = inspect(home, exe)
        if args.action == "doctor":
            report["checks"] = doctor(home, report)
        render_report(report, as_json=args.as_json)
        healthy = report_healthy(args.action, report)
        if (
            healthy
            and args.action == "start"
            and not args.as_json
            and not args.detach
            and (args.follow or (sys.stdin.isatty() and sys.stdout.isatty()))
        ):
            from .activity import watch

            return watch(home, details=True)
        return 0 if healthy else 2
    except (OSError, ValueError, CommandError) as exc:
        # Do not echo raw state contents, credential errors, or process output.
        message = (
            str(exc)
            if isinstance(exc, CommandError)
            else "Cannot inspect/start this installation. Check its path, integrity and local state."
        )
        if args.as_json:
            print(json.dumps(dict(error=message, chatgpt_connection_verified=False)))
        else:
            print("UDB: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
