"""Real HTTP download/install smoke tests in disposable Windows directories."""

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("installer", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    installer = args.installer.resolve()
    windows = Path(os.environ["SystemRoot"])
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UDB_HOME",
        "CONTROL_PLANE_API_KEY",
        "TUNNEL_CLIENT_PROFILE_DIR",
    ):
        env.pop(name, None)
    env["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    env["PYTHONIOENCODING"] = "utf-8"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="udb-installer-") as folder:
        root = Path(folder).resolve()
        # The Windows PowerShell launcher may inherit a PowerShell 7 module path.
        env["PSModulePath"] = str(root / "no extra PowerShell modules")
        feed = root / "feed"
        feed.mkdir()
        archive = feed / "udb-windows-x64.zip"
        shutil.copyfile(args.archive, archive)
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        sums = feed / "SHA256SUMS"
        sums.write_text(sha + "  udb-windows-x64.zip\n", encoding="ascii")
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Handler, directory=str(feed))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        install = root / "application with spaces"
        home = root / "data with spaces"
        home.mkdir()
        sentinel = home / "keep.txt"
        sentinel.write_text("existing owner data")

        def run_install(target=install, expect_success=True):
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(installer),
                    "-FeedUrl",
                    url,
                    "-InstallRoot",
                    str(target),
                    "-DataHome",
                    str(home),
                    "-AllowLocalFeed",
                    "-NoPath",
                    "-NoStart",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert (result.returncode == 0) is expect_success, (
                result.stdout + result.stderr
            )
            return result

        def app(*arguments, image=None, success=True):
            result = subprocess.run(
                [str(image or exe), "--home", str(home), *arguments],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert not success or result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        def publish_variant(note, replacement=None):
            # Change only test-package public bytes; never write to owner state.
            with zipfile.ZipFile(args.archive) as original:
                files = {name: original.read(name) for name in original.namelist()}
            files["UDB/STANDALONE.md"] += ("\n" + note).encode()
            if replacement is not None:
                files["UDB/udb.exe"] = replacement
            files["UDB/STANDALONE-SHA256SUMS"] = "".join(
                hashlib.sha256(content).hexdigest()
                + "  "
                + name.removeprefix("UDB/")
                + "\n"
                for name, content in sorted(files.items())
                if name != "UDB/STANDALONE-SHA256SUMS"
            ).encode("ascii")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                for name, content in files.items():
                    package.writestr(name, content)
            sums.write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest()
                + "  udb-windows-x64.zip\n",
                encoding="ascii",
            )

        try:
            run_install()
            record_path = install / "install.json"
            first = json.loads(record_path.read_text(encoding="utf-8"))
            exe = Path(first["exe"])
            assert exe.is_file() and first["home"] == str(home)
            assert sentinel.read_text() == "existing owner data"
            assert not (home / "state/computer-access.dpapi").exists()
            assert not (home / "state/connection.json").exists()
            assert not (home / "state/manager.lock").exists()
            mcp = json.loads((install / "mcp.json").read_text(encoding="utf-8"))[
                "mcpServers"
            ]["universal-desktop-bridge"]
            assert mcp["command"] == str(exe)
            probe = subprocess.run(
                [str(exe), "runtime", "--probe"],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
            runtime = json.loads(probe.stdout)
            assert (
                runtime["python_embedded"] and not runtime["source_checkout_required"]
            )
            assert not runtime["desktop_commander_required"]
            assert all(item["ok"] for item in runtime["component_probes"].values())
            cli = subprocess.run(
                [
                    str(windows / "System32/cmd.exe"),
                    "/d",
                    "/c",
                    str(install / "bin/udb.cmd"),
                    "--version",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert cli.returncode == 0 and cli.stdout.strip() == first["version"], (
                cli.stderr
            )
            setup_home = root / "refused setup"
            setup = subprocess.run(
                [str(exe), "setup", "--home", str(setup_home)],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert setup.returncode != 0 and not setup_home.exists()
            run_install()
            assert json.loads(record_path.read_text(encoding="utf-8")) == first
            assert len(list((install / "releases").iterdir())) == 1
            # Actual running offline agent survives a release switch and rollback.
            preference = home / "state/startup-preferences.json"
            preference.parent.mkdir(exist_ok=True)
            preference.write_text('{"schema":1,"mode":"offline"}')
            app("start", "--detach", "--json", "--wait", "60")
            before_mode = preference.read_bytes()
            publish_variant("Synthetic update test")
            run_install()
            second = json.loads(record_path.read_text(encoding="utf-8"))
            assert second["exe"] != first["exe"] and second["previous"] == first
            switched = app("status", "--json", image=second["exe"])
            assert (
                switched["manager_running"] and switched["connection_mode"] == "offline"
            )
            assert (
                preference.read_bytes() == before_mode
                and sentinel.read_text() == "existing owner data"
            )
            rollback = app("rollback", "--json", image=second["exe"])
            assert rollback["home_preserved"]
            restored = json.loads(record_path.read_text(encoding="utf-8"))
            assert restored["exe"] == first["exe"]
            assert app("status", "--json")["manager_running"]
            assert preference.read_bytes() == before_mode

            # Build a synthetic console shim that fails only startup. Other commands
            # delegate to the real copied EXE; this tests failure AFTER the switch.
            shim = root / "failed-start.exe"
            image_literal = (
                '"' + str(exe).replace("\\", "\\\\").replace('"', '\\"') + '"'
            )
            code = r"""
using System;
using System.Diagnostics;
using System.Linq;
public class FailedStart {
    public static int Main(string[] args) {
        if (args.Contains("start") && !args.Contains("--help")) {
            Console.WriteLine("{\"manager_running\":false,\"connection_mode\":\"offline\"}");
            return 2;
        }
        // Only test arguments: paths never end with a slash or contain quotes.
        string quoted = String.Join(" ", args.Select(x => "\"" + x + "\""));
        var process = Process.Start(new ProcessStartInfo(IMAGE, quoted) { UseShellExecute = false });
        process.WaitForExit();
        return process.ExitCode;
    }
}
""".replace("IMAGE", image_literal)
            compile_script = root / "compile.ps1"
            compile_script.write_text(
                "$ErrorActionPreference='Stop'\nAdd-Type -ReferencedAssemblies 'System.dll','System.Core.dll' -TypeDefinition @'\n"
                + code
                + "\n'@ -OutputType ConsoleApplication -OutputAssembly '"
                + str(shim).replace("'", "''")
                + "'\n",
                encoding="utf-8",
            )
            compiled = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(compile_script),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert compiled.returncode == 0, compiled.stdout + compiled.stderr
            old_record_bytes = record_path.read_bytes()
            old_mcp_bytes = (install / "mcp.json").read_bytes()
            publish_variant("Synthetic startup failure", shim.read_bytes())
            run_install(expect_success=False)
            assert json.loads(record_path.read_text(encoding="utf-8")) == json.loads(
                old_record_bytes
            )
            assert (install / "mcp.json").read_bytes() == old_mcp_bytes
            recovered = app("status", "--json")
            assert (
                recovered["manager_running"]
                and recovered["connection_mode"] == "offline"
            )
            assert preference.read_bytes() == before_mode
            app("stop", "--wait", "60", "--json")
            assert not (home / "state/computer-access.dpapi").exists()
            sums.write_text("0" * 64 + "  udb-windows-x64.zip\n", encoding="ascii")
            run_install(expect_success=False)
            assert (
                json.loads(record_path.read_text(encoding="utf-8"))["exe"]
                == first["exe"]
            )
            # A valid checksum cannot make a traversing archive safe.
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("UDB/../escape.txt", "must not escape")
            sums.write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest()
                + "  udb-windows-x64.zip\n",
                encoding="ascii",
            )
            attack = root / "attack target"
            run_install(target=attack, expect_success=False)
            assert (
                not list(attack.glob(".stage-*")) and not (root / "escape.txt").exists()
            )
            assert not (attack / "install.json").exists()
            unknown = root / "unrelated"
            unknown.mkdir()
            (unknown / "keep.txt").write_text("preserve")
            run_install(target=unknown, expect_success=False)
            assert (unknown / "keep.txt").read_text() == "preserve"
            print(
                json.dumps(
                    dict(
                        result="PASS",
                        http_download=True,
                        clean_system_path=True,
                        no_python_or_node=True,
                        bundled_helper_probes=True,
                        paths_with_spaces=True,
                        launcher=True,
                        mcp_config=True,
                        reinstall_idempotent=True,
                        preserves_owner_data=True,
                        checksum_rejection=True,
                        traversal_rejection=True,
                        unrelated_folder_preserved=True,
                        redirected_setup_refused=True,
                        running_offline_update=True,
                        manual_rollback=True,
                        automatic_failure_rollback=True,
                        owner_access_not_granted=True,
                        user_path_not_changed_by_test=True,
                    )
                )
            )
        finally:
            if (install / "install.json").exists():
                active = json.loads((install / "install.json").read_text("utf-8"))
                subprocess.run(
                    [
                        active["exe"],
                        "--home",
                        str(home),
                        "stop",
                        "--wait",
                        "60",
                        "--json",
                    ],
                    env=env,
                    capture_output=True,
                    timeout=90,
                )
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
