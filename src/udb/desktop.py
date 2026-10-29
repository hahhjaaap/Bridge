"""Local desktop process owner. No methods here are exported through MCP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .connection import is_tunnel_id, normalize_tunnel_id
from .core import Agent, PolicyError, reject_links
from .credentials import load_key, save_key
from .windows_paths import system_executable

HEALTH_URL_NAME = "tunnel-health.url"
UNREADY_RESTART_SECONDS = 90
CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command(home: Path, *args):
    from .launcher import worker_command

    return worker_command("core", [*args, "--home", str(home)])


def mcp_command(home: Path):
    """Use the installed stdio manifest, retaining the legacy default.

    This selects a transport process; it cannot create owner permissions.
    Invalid existing manifests fail closed rather than silently downgrade.
    """
    # A frozen distribution owns its worker executable; legacy manifests may
    # point at a development venv that does not exist on the destination PC.
    if getattr(sys, "frozen", False):
        from .config import serve_options
        from .launcher import command as unified_command

        return unified_command(serve_options(home))
    manifest = home / "mcp.json"
    if not manifest.exists():
        from .config import serve_options
        from .launcher import command as unified_command

        return unified_command(serve_options(home))
    reject_links(manifest)
    with manifest.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise PolicyError("MCP manifest exceeds size limit")
    try:
        config = json.loads(raw.decode("utf-8-sig"))["mcpServers"][
            "universal-desktop-bridge"
        ]
        exe, args = config["command"], config["args"]
        if config.get("type", "stdio") != "stdio" or not isinstance(exe, str):
            raise ValueError()
        if (
            not isinstance(args, list)
            or len(args) > 128
            or any(not isinstance(a, str) or "\0" in a for a in args)
        ):
            raise ValueError()
        argv = [a.replace("${PLUGIN_ROOT}", str(home)) for a in [exe, *args]]
        target = Path(argv[0])
        if not target.is_absolute() or not target.is_file() or "\0" in argv[0]:
            raise ValueError()
        reject_links(target)
        return argv
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise PolicyError("Invalid installed stdio MCP manifest") from None


class DesktopController:
    def __init__(self, home: Path, monitor=True):
        self.home = Path(home)
        self.agent = Agent(self.home)
        self.children = {}
        self.handles = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.ready = False
        self.error = ""
        self.host_service = None
        self.tunnel_wanted = False
        self._connecting = False
        self._generation = 0
        self._retry_attempt = 0
        self._retry_at = 0.0
        self._unready_since = None
        self._last_probe_at = None
        self._recovery_count = 0
        self._last_recovery = ""
        if monitor:
            threading.Thread(target=self._monitor, daemon=True).start()
            threading.Thread(target=self._sandbox_monitor, daemon=True).start()

    def start_host(self):
        if self.host_service is None:
            from .host_service import HostService

            self.host_service = HostService(self.agent)

    def config(self):
        p = self.agent.state / "connection.json"
        return json.loads(p.read_text("utf-8-sig")) if p.exists() else {}

    def settings(self):
        p = self.agent.state / "gui.json"
        try:
            return json.loads(p.read_text("utf-8-sig")) if p.exists() else {}
        except (ValueError, OSError):
            return {}

    def save_settings(self, settings):
        # Only presentation/launch preferences; never credentials.
        safe = {k: bool(settings.get(k)) for k in ("motion", "autoconnect")}
        (self.agent.state / "gui.json").write_text(json.dumps(safe), encoding="utf-8")

    def configure(self, tunnel_id, key=""):
        try:
            tunnel_id = normalize_tunnel_id(tunnel_id)
        except ValueError as exc:
            raise PolicyError(str(exc)) from None
        if key:
            save_key(key)
        elif not self.config().get("tunnel_id"):
            raise PolicyError("Cần nhập runtime API key khi cấu hình lần đầu.")
        (self.agent.state / "connection.json").write_text(
            json.dumps({"tunnel_id": tunnel_id}), encoding="utf-8"
        )

    def alive(self, name):
        with self.lock:
            p = self.children.get(name)
            return p is not None and p.poll() is None

    def start_process(self, name, argv, env=None):
        with self.lock:
            if self.closed.is_set():
                raise PolicyError("Ứng dụng đang đóng.")
            if self.alive(name):
                return
            previous = self.handles.pop(name, None)
            if previous:
                previous.close()
            log = (self.agent.state / (name + ".log")).open("ab")
            try:
                self.children[name] = subprocess.Popen(
                    argv,
                    cwd=self.home,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    shell=False,
                    creationflags=CREATE_FLAGS,
                )
            except Exception:
                log.close()
                raise
            self.handles[name] = log

    def start_tunnel(self, *, _retry_generation=None):
        with self.lock:
            if _retry_generation is not None and (
                not self.tunnel_wanted or _retry_generation != self._generation
            ):
                return
            if self.closed.is_set():
                raise PolicyError("Ứng dụng đang đóng.")
            if not self.tunnel_wanted:
                self._generation += 1
                self.tunnel_wanted = True
                self._retry_attempt = 0
            if self._connecting or self.alive("tunnel"):
                return
            generation = self._generation
            self._connecting = True
        try:
            self._launch_tunnel(generation)
        except Exception:
            with self.lock:
                if generation == self._generation and self.tunnel_wanted:
                    # Do not forward subprocess output or credential exceptions.
                    self.error = (
                        "Kết nối thất bại; sẽ thử lại. Kiểm tra cài đặt kết nối."
                    )
            raise PolicyError(
                "Kết nối thất bại. Kiểm tra cấu hình và trạng thái mạng trong ứng dụng."
            ) from None
        finally:
            with self.lock:
                self._connecting = False
                if generation == self._generation and self.tunnel_wanted:
                    delay = min(30, 2 ** min(self._retry_attempt, 5))
                    self._retry_attempt += 1
                    self._retry_at = time.monotonic() + delay

    def _launch_tunnel(self, generation):
        cfg = self.config()
        if not is_tunnel_id(cfg.get("tunnel_id")):
            raise PolicyError("Mở Cài đặt kết nối và nhập Tunnel ID trước.")
        from .components import component_path

        binary = component_path("tunnel-client", self.home)
        if binary is None:
            raise PolicyError(
                "This build has no tunnel component. Use a release that bundles tunnel-client."
            )
        try:
            secret = load_key()
        except Exception:
            raise PolicyError(
                "Chưa đọc được runtime key. Nhập lại trong Cài đặt kết nối; không gửi key vào Chat."
            ) from None
        work = self.agent.state / "tunnel"
        work.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["CONTROL_PLANE_API_KEY"] = secret
        env["TUNNEL_CLIENT_PROFILE_DIR"] = str(work)
        cmdline = subprocess.list2cmdline(
            [a.replace("\\", "/") for a in mcp_command(self.home)]
        )
        argv = [
            str(binary),
            "init",
            "--force",
            "--profile-dir",
            str(work),
            "--sample",
            "sample_mcp_stdio_local",
            "--profile",
            "universal-desktop-bridge",
            "--health-listen-addr",
            "127.0.0.1:0",
            "--tunnel-id",
            cfg["tunnel_id"],
            "--mcp-command",
            cmdline,
        ]
        result = subprocess.run(
            argv,
            cwd=work,
            env=env,
            capture_output=True,
            timeout=30,
            creationflags=CREATE_FLAGS,
        )
        if result.returncode:
            # Never forward captured CLI output: it may contain authentication context.
            raise PolicyError(
                "Khởi tạo tunnel chưa thành công. Kiểm tra ID và quyền Tunnels Read + Use."
            )
        try:
            with self.lock:
                # A local Disconnect/Close during init invalidates this launch.
                if (
                    self.closed.is_set()
                    or not self.tunnel_wanted
                    or generation != self._generation
                ):
                    return
                self.ready = False
                self._unready_since = time.monotonic()
                self.error = ""
                health_file = self.agent.state / HEALTH_URL_NAME
                health_file.unlink(missing_ok=True)
                self.start_process(
                    "tunnel",
                    [
                        str(binary),
                        "run",
                        "--mcp.stdio-send-initialized-notification",
                        "--profile",
                        "universal-desktop-bridge",
                        "--health.url-file",
                        str(health_file),
                    ],
                    env,
                )
        finally:
            secret = None
            env.pop("CONTROL_PLANE_API_KEY", None)

    def start_local(self):
        self.start_process("mcp", command(self.home, "--serve"))

    def stop_process(self, name):
        with self.lock:
            if name == "tunnel":
                self.tunnel_wanted = False
                self._generation += 1
                self.ready = False
                self._retry_at = 0.0
                self.error = ""
            p = self.children.get(name)
            if p is None or p.poll() is not None:
                return
            # Only a still-live child owned by THIS controller, never arbitrary user PIDs.
            if os.name == "nt":
                subprocess.run(
                    [system_executable("taskkill.exe"), "/PID", str(p.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=CREATE_FLAGS,
                    timeout=15,
                )
            else:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if name == "tunnel":
            self.ready = False

    def _sandbox_monitor(self):
        while not self.closed.wait(1):
            try:
                self.agent.pump_sandbox_queue_local()
                self.agent.pump_media_queue_local()
            except Exception as e:
                self.error = "Automation queue: " + str(e)

    def _monitor(self):
        from .owner_command import NoRedirect

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        while not self.closed.wait(1.5):
            try:
                self._monitor_once(opener)
            except Exception:
                # A failed local probe/child stop must not kill the watchdog thread.
                self.ready = False
                self.error = "Connection monitor failed; retrying local checks."

    def _monitor_once(self, opener, now=None):
        from .owner_command import tunnel_ready

        now = time.monotonic() if now is None else now
        with self.lock:
            generation = self._generation
            child = self.children.get("tunnel")
        if not self.alive("tunnel"):
            self.ready = False
            self._retry_if_due(now)
            return
        ready = tunnel_ready(self.home, opener)
        with self.lock:
            # Discard a late HTTP response from a previous process/connection.
            if generation != self._generation or child is not self.children.get(
                "tunnel"
            ):
                return
            self.ready = ready and self.tunnel_wanted and self.alive("tunnel")
            if self.ready:
                self._unready_since = None
                self._retry_attempt = 0
                self._retry_at = 0.0
                self.error = ""
            else:
                # Allow a full recovery grace period after suspend or a delayed monitor.
                if self._unready_since is None or (
                    self._last_probe_at is not None and now - self._last_probe_at > 30
                ):
                    self._unready_since = now
                if (
                    self.tunnel_wanted
                    and not self.closed.is_set()
                    and not self._connecting
                    and now - self._unready_since >= UNREADY_RESTART_SECONDS
                ):
                    self._recover_unready(generation)
            self._last_probe_at = now

    def _recover_unready(self, generation):
        # Called with the controller lock; Stop cannot race a reconnect intent.
        if generation != self._generation or not self.tunnel_wanted:
            return
        try:
            self.stop_process("tunnel")  # Only this controller's owned child tree.
        except (OSError, subprocess.SubprocessError):
            if not self.closed.is_set():
                self.tunnel_wanted = True
                self._unready_since = time.monotonic()
                self.error = "Could not stop the unready tunnel; retrying local checks."
            return
        if self.alive("tunnel") or self.closed.is_set():
            if not self.closed.is_set():
                self.tunnel_wanted = True
                self._unready_since = time.monotonic()
            return
        self.tunnel_wanted = True
        self._recovery_count += 1
        self._last_recovery = "tunnel_unready"
        self._unready_since = None
        self._retry_at = time.monotonic() + min(30, 2 ** min(self._retry_attempt, 5))
        self._retry_attempt += 1
        self.error = (
            "Tunnel remained unready; reconnect scheduled. No tool call is replayed."
        )
        from .activity import Activity

        Activity(self.home).emit(
            "connection",
            "tunnel",
            status="RETRY",
            details={"action": "recover_unready"},
        )

    def _retry_if_due(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            due = (
                self.tunnel_wanted
                and not self._connecting
                and not self.closed.is_set()
                and not self.alive("tunnel")
                and now >= self._retry_at
            )
            generation = self._generation
        if due:
            try:
                self.start_tunnel(_retry_generation=generation)
            except Exception:
                pass  # start_tunnel stores a sanitized status and bounded backoff.

    def status(self):
        from .sandbox import available

        return {
            "tunnel_running": self.alive("tunnel"),
            "ready": self.ready,
            "tunnel_wanted": self.tunnel_wanted,
            "connecting": self._connecting,
            "retry_in_seconds": max(0, round(self._retry_at - time.monotonic(), 1))
            if self.tunnel_wanted
            else 0,
            "connection_error": self.error,
            "recovery_count": self._recovery_count,
            "last_recovery": self._last_recovery,
            "local_running": self.alive("mcp"),
            "sandbox": available(),
        }

    def close(self):
        self.closed.set()
        failures = []
        if self.host_service is not None:
            try:
                self.host_service.close()
            except Exception:
                failures.append("host sessions")
            else:
                self.host_service = None
        from .desktop_vm import close_owned

        try:
            close_owned(self.agent)
        except Exception:
            failures.append("Sandbox")
        for name in list(self.children):
            try:
                self.stop_process(name)
            except Exception:
                failures.append(name)
        for name, handle in list(self.handles.items()):
            try:
                handle.close()
            except Exception:
                failures.append(name + " log")
            else:
                self.handles.pop(name, None)
        if failures:
            raise PolicyError(
                "Cleanup incomplete; retry closing: " + ", ".join(failures)
            )
