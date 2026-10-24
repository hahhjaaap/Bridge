"""Offline regression tests: synthetic installations, processes and loopback HTTP."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "command" / "udb_cli.py"
spec = importlib.util.spec_from_file_location("udb_one_command", ENTRY)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "Nguyễn Ánh & UDB"
        self.home.mkdir()
        self.exe = self.home / "UniversalDesktopBridge.exe"
        self.exe.write_bytes(b"SYNTHETIC-NOT-EXECUTABLE")
        (self.home / "state").mkdir()

    def write(self, name, value):
        path = self.home / "state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def report(self, running=True, ready=True):
        return cli.inspect(self.home, self.exe, running=lambda *a: running, ready=lambda *a: ready)


class InstallationTests(Fixture):
    def test_direct_installation_preserves_unicode(self):
        self.assertEqual(cli.executable(self.home), self.exe)

    def managed(self, active="0.5.0-dev.3-abcdef"):
        target = self.home / "releases" / active / self.exe.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic-managed-exe")
        info = dict(active=active, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        (self.home / "installation.json").write_text(json.dumps(info), encoding="utf-8-sig")
        return target

    def test_managed_installation_uses_active_release(self):
        target = self.managed()
        self.assertEqual(target, cli.executable(self.home))

    def test_tampered_managed_executable_refused(self):
        target = self.managed()
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(cli.CommandError, "integrity"):
            cli.executable(self.home)

    def test_managed_rollback_is_followed_on_next_command(self):
        current = self.managed()
        previous = self.managed("0.4.9-old")
        self.assertNotEqual(current, previous)
        self.assertEqual(cli.executable(self.home), previous)

    def test_bad_metadata_cannot_fall_back_to_unchecked_exe(self):
        for active in ("../outside", "..", ".", "C:\\outside", None, 7):
            with self.subTest(active=active):
                (self.home / "installation.json").write_text(json.dumps(dict(active=active, sha256="a" * 64)))
                with self.assertRaises(cli.CommandError):
                    cli.executable(self.home)

    def test_symlink_installation_refused(self):
        link = Path(self.temp.name) / "linked"
        try:
            link.symlink_to(self.home, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(cli.CommandError):
            cli.executable(link)

    def test_bounded_reads_and_object_format(self):
        path = self.home / "huge"
        path.write_bytes(b"x" * 130)
        with self.assertRaises(cli.CommandError):
            cli.read_text(path, 128)
        path.write_text("[]")
        with self.assertRaises(cli.CommandError):
            cli.read_object(path)


class ReadinessTests(Fixture):
    def test_disabled_background_does_not_block_connection(self):
        report = self.report()
        self.assertTrue(report["local_tunnel_ready"])
        self.assertEqual(report["background"], "disabled")
        self.assertFalse(report["chatgpt_connection_verified"])

    def test_stale_health_without_live_manager_is_not_ready(self):
        calls = []
        report = cli.inspect(self.home, self.exe, running=lambda *a: False,
                             ready=lambda *a: calls.append(True))
        self.assertFalse(report["local_tunnel_ready"])
        self.assertEqual(calls, [])

    def test_disabled_settings_override_old_worker_state(self):
        self.write("host-settings.json", dict(enabled=False))
        self.write("host/status.json", dict(worker_ready=True, resources=dict(at=time.time())))
        self.assertEqual(cli.background_state(self.home), "disabled")

    def test_stale_future_and_malformed_worker_timestamps(self):
        self.write("host-settings.json", dict(enabled=True))
        for stamp in (0, 1000, True, "now", float("nan"), float("inf"), None):
            with self.subTest(stamp=stamp):
                self.write("host/status.json", dict(worker_ready=True, resources=dict(at=stamp)))
                self.assertEqual(cli.background_state(self.home, now=100), "unavailable")

    def test_background_pause_is_reported_separately(self):
        self.write("host-settings.json", dict(enabled=True))
        self.write("host/status.json", dict(worker_ready=True, resources=dict(at=99, paused=True)))
        self.assertEqual(cli.background_state(self.home, now=100), "paused")

    def test_manager_pid_must_match_selected_executable(self):
        (self.home / "state" / "manager.lock").write_text("123\nUniversalDesktopBridge\nWORKSTATION\n")
        self.assertTrue(cli.manager_running(self.home, self.exe, image=lambda p: str(self.exe)))
        self.assertFalse(cli.manager_running(self.home, self.exe, image=lambda p: "/other/application.exe"))
        self.assertFalse(cli.manager_running(self.home, self.exe, image=lambda p: None))

    def test_invalid_manager_lock_is_not_running(self):
        for content in ("", "0", "-1", "99999999999", "not-a-pid", "[]"):
            with self.subTest(content=content):
                (self.home / "state" / "manager.lock").write_text(content)
                self.assertFalse(cli.manager_running(self.home, self.exe, image=lambda p: str(self.exe)))

    def test_live_upgraded_manager_requires_listed_unchanged_sibling(self):
        (self.home/'state/manager.lock').write_text('123\n')
        candidate=self.home/'UniversalDesktopBridge.pre-upgrade.running.exe'
        candidate.write_bytes(b'synthetic-old-image')
        config=self.home/'UDB-Command/running-images.json';config.parent.mkdir()
        config.write_text(json.dumps({candidate.name:hashlib.sha256(candidate.read_bytes()).hexdigest()}))
        self.assertTrue(cli.manager_running(self.home,self.exe,image=lambda p:str(candidate)))
        candidate.write_bytes(b'tampered')
        self.assertFalse(cli.manager_running(self.home,self.exe,image=lambda p:str(candidate)))
        other=self.home/'UniversalDesktopBridge.other.running.exe';other.write_bytes(b'other')
        self.assertFalse(cli.manager_running(self.home,self.exe,image=lambda p:str(other)))
        external=Path(self.temp.name)/candidate.name;external.write_bytes(b'synthetic-old-image')
        self.assertFalse(cli.manager_running(self.home,self.exe,image=lambda p:str(external)))

    @unittest.skipUnless(os.name == "nt", "Native Windows process-image API")
    def test_native_windows_process_image_matches_current_process(self):
        (self.home / "state" / "manager.lock").write_text(str(os.getpid()) + "\n")
        actual = cli.process_image(os.getpid())
        # Windows venv python.exe may redirect to the base interpreter process.
        expected = {os.path.normcase(os.path.abspath(p)) for p in
                    (sys.executable, getattr(sys, "_base_executable", sys.executable))}
        self.assertIn(os.path.normcase(os.path.abspath(actual or "")), expected)
        self.assertTrue(cli.manager_running(self.home, Path(actual)))

    def test_doctor_does_not_return_tunnel_id_or_secret(self):
        tunnel = "tunnel_" + "a" * 32
        self.write("connection.json", dict(tunnel_id=tunnel, synthetic_key="DO-NOT-DISPLAY"))
        checks = cli.doctor(self.home, self.report())
        self.assertNotIn(tunnel, json.dumps(checks))
        self.assertNotIn("DO-NOT-DISPLAY", json.dumps(checks))
        self.assertTrue(checks[0]["ok"])
        self.assertIsNone(checks[-1]["ok"])


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests.append(self.path)
                if owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                else:
                    self.send_response(200 if owner.mode == "ready" else 503)
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.mode = "ready"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown(); self.server.server_close(); thread.join()
        self.addCleanup(cleanup)
        self.path = self.home / "state" / "tunnel-health.url"
        self.path.write_text(f"http://127.0.0.1:{self.server.server_port}")

    def test_real_loopback_ready_and_unavailable(self):
        self.assertTrue(cli.tunnel_ready(self.home))
        self.mode = "unavailable"
        self.assertFalse(cli.tunnel_ready(self.home))
        self.assertEqual(self.requests, ["/readyz", "/readyz"])

    def test_redirect_is_not_followed(self):
        self.mode = "redirect"
        self.assertFalse(cli.tunnel_ready(self.home))
        self.assertEqual(self.requests, ["/readyz"])

    def test_environment_proxy_is_ignored(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
            self.assertTrue(cli.tunnel_ready(self.home))

    def test_invalid_health_urls_never_make_requests(self):
        for value in ("https://127.0.0.1:12", "http://localhost:12", "http://example.test:12",
                      "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:12/x",
                      "http://127.0.0.1:12@external.test", "x" * 129):
            with self.subTest(value=value):
                self.path.write_text(value)
                self.assertFalse(cli.tunnel_ready(self.home))
        self.assertEqual(self.requests, [])


class StartupTests(Fixture):
    def test_test_action_uses_only_selected_exe_without_shell_or_new_grant(self):
        with patch.object(cli,'os',type('OS',(),{'name':'nt'})()),patch.object(cli,'start',return_value=dict(manager_running=True)), \
                patch.object(cli.subprocess,'run',return_value=type('R',(),{'returncode':2})()) as run:
            self.assertEqual(cli.main(['test','--home',str(self.home)]),2)
        run.assert_called_once_with([str(self.exe.resolve()),'--probe','--home',str(self.home.resolve())],shell=False)

    def test_reuses_running_manager_without_launching_any_process(self):
        def forbidden(*a, **kw):
            self.fail("Duplicate manager launched")
        report = cli.start(self.home, self.exe, snapshot=lambda *a: self.report(), spawn=forbidden)
        self.assertTrue(report["local_tunnel_ready"])

    def test_launches_selected_exe_once_with_argument_array(self):
        calls, count = [], [0]
        def snapshot(*a):
            count[0] += 1
            return self.report(running=count[0] > 1, ready=count[0] > 1)
        cli.start(self.home, self.exe, snapshot=snapshot, spawn=lambda *a, **kw: calls.append((a, kw)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], [str(self.exe), "--home", str(self.home), "--headless"])
        self.assertFalse(calls[0][1]["shell"])

    def test_running_disconnected_manager_is_not_killed_or_relaunched(self):
        def forbidden(*a, **kw):
            self.fail("Existing application relaunched")
        report = cli.start(self.home, self.exe, wait=0, snapshot=lambda *a: self.report(ready=False), spawn=forbidden)
        self.assertFalse(report["local_tunnel_ready"])
        self.assertIn("legacy UDB window", report["next_action"])

    def test_timeout_stops_without_resetting_settings(self):
        ticks = [0.0]
        def pause(delay):
            ticks[0] += delay
        self.write("gui.json", {"autoconnect": False, "owner_setting": "preserve"})
        self.write("host-settings.json", {"enabled": False})
        before = {p: p.read_bytes() for p in (self.home / "state").iterdir()}
        report = cli.start(self.home, self.exe, wait=1, snapshot=lambda *a: self.report(ready=False),
                           clock=lambda: ticks[0], pause=pause)
        self.assertFalse(report["local_tunnel_ready"])
        self.assertLessEqual(ticks[0], 1.01)
        self.assertEqual(before, {p: p.read_bytes() for p in (self.home / "state").iterdir()})

    def test_gate_blocks_concurrent_startup_and_releases_after_failure(self):
        with cli.launch_gate(self.home):
            with self.assertRaises(cli.CommandError):
                with cli.launch_gate(self.home, timeout=0):
                    self.fail("Concurrent startup entered")
        with self.assertRaises(RuntimeError):
            with cli.launch_gate(self.home):
                raise RuntimeError("synthetic failure")
        with cli.launch_gate(self.home, timeout=0):
            pass

    def test_status_json_cli_is_read_only_and_never_executes_fake_exe(self):
        self.write("gui.json", dict(autoconnect=False))
        def inventory():
            return sorted((str(p.relative_to(self.home)), p.read_bytes()) for p in self.home.rglob("*") if p.is_file())
        before = inventory()
        result = subprocess.run([sys.executable, str(ENTRY), "status", "--json", "--home", str(self.home)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["chatgpt_connection_verified"])
        self.assertEqual(before, inventory())

    def test_missing_installation_returns_json_error(self):
        self.exe.unlink()
        result = subprocess.run([sys.executable, str(ENTRY), "doctor", "--json", "--home", str(self.home)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Application not found", json.loads(result.stdout)["error"])

    def test_ready_status_exit_zero_even_when_background_is_disabled(self):
        with patch.object(cli, "inspect", return_value=self.report()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["status", "--home", str(self.home)]), 0)


if __name__ == "__main__":
    unittest.main()
