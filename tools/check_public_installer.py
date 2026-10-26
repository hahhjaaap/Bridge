"""Install the published release on Windows without Python on the child PATH."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

API = "https://api.github.com/repos/hahhjaaap/Universal-Desktop-Bridge/releases/latest"
FEED = "https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download"


def fetch(url, limit):
    request = urllib.request.Request(
        url, headers={"User-Agent": "UDB-release-acceptance"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Public release response exceeds its limit")
    return data


@contextmanager
def temporary_installation():
    directory = tempfile.TemporaryDirectory(prefix="UDB public acceptance ")
    try:
        yield Path(directory.name).resolve()
    finally:
        # Windows can retain an image mapping briefly after its bootloader exits.
        for attempt in range(50):
            try:
                directory.cleanup()
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--release-tag")
    args = parser.parse_args()
    if os.name != "nt":
        raise ValueError("Published Windows acceptance requires Windows")
    if args.release_tag and not re.fullmatch(r"v\d+\.\d+\.\d+", args.release_tag):
        raise ValueError("Expected a stable release tag such as v0.9.2")
    api = (
        API
        if not args.release_tag
        else API.removesuffix("/latest") + "/tags/" + args.release_tag
    )
    feed = (
        FEED
        if not args.release_tag
        else FEED.removesuffix("/latest/download") + "/download/" + args.release_tag
    )
    release = json.loads(fetch(api, 1024 * 1024))
    if release["draft"] or release["prerelease"]:
        raise ValueError("Expected a published stable release")
    assets = {item["name"]: item for item in release["assets"]}
    required = {"install.ps1", "SHA256SUMS", "udb-windows-x64.zip"}
    if not required <= assets.keys():
        raise ValueError("The public release is incomplete")

    def verified_asset(name, limit):
        data = fetch(feed + "/" + name, limit)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if assets[name].get("digest") != digest:
            raise ValueError("Public release asset digest mismatch: " + name)
        return data

    script = verified_asset("install.ps1", 1024 * 1024)
    sums = verified_asset("SHA256SUMS", 65536).decode("ascii")
    archive_sha = assets["udb-windows-x64.zip"]["digest"].removeprefix("sha256:")
    if sums.strip() != archive_sha + "  udb-windows-x64.zip":
        raise ValueError("Release checksum and GitHub asset metadata differ")
    windows = Path(os.environ["SystemRoot"])
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("_PYI_")
        and key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "UDB_HOME",
            "CONTROL_PLANE_API_KEY",
            "TUNNEL_CLIENT_PROFILE_DIR",
        }
    }
    env["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    env["PYTHONIOENCODING"] = "utf-8"
    assert shutil.which("python", path=env["PATH"]) is None
    assert shutil.which("node", path=env["PATH"]) is None

    with temporary_installation() as root:
        env["PSModulePath"] = str(root / "no additional PowerShell modules")
        install = root / "Application with spaces"
        home = root / "Owner data with spaces"
        installer = root / "install.ps1"
        installer.write_bytes(script)

        def run(command, timeout=120, success=True):
            result = subprocess.run(
                list(map(str, command)),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            if success and result.returncode:
                raise RuntimeError((result.stdout + result.stderr)[-12000:])
            return result

        print("Downloading and installing the public HTTPS release...", flush=True)
        run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                installer,
                "-InstallRoot",
                install,
                "-DataHome",
                home,
                "-NoStart",
                "-NoPath",
            ],
            timeout=3000,
        )
        record = json.loads((install / "install.json").read_text("utf-8"))
        exe = Path(record["exe"])
        assert record["product"] == "UniversalDesktopBridge" and record["schema"] == 1
        assert exe.is_file() and exe.resolve().is_relative_to(install)
        assert Path(record["home"]) == home and record["archive_sha256"] == archive_sha
        version = run([exe, "--version"]).stdout.strip()
        assert release["tag_name"] == "v" + version == "v" + record["version"]
        runtime = json.loads(run([exe, "--home", home, "runtime", "--probe"]).stdout)
        assert runtime["python_embedded"] and not runtime["source_checkout_required"]
        assert not runtime["desktop_commander_required"]
        assert all(row["ok"] for row in runtime["component_probes"].values())
        assert all(runtime["office_modules"].values())
        launcher = run(
            [
                windows / "System32/cmd.exe",
                "/d",
                "/c",
                install / "bin/udb.cmd",
                "--version",
            ]
        )
        assert launcher.stdout.strip() == version
        mcp = json.loads((install / "mcp.json").read_text("utf-8"))
        assert mcp["mcpServers"]["universal-desktop-bridge"]["command"] == str(exe)
        setup_home = root / "Redirected setup must not create this"
        refused = run([exe, "--home", setup_home, "setup"], success=False)
        assert refused.returncode and not setup_home.exists()
        assert "attended Windows terminal" in refused.stdout + refused.stderr
        saved_local_mode = tuple(map(int, version.split("."))) >= (0, 9, 3)
        mode_flags = ["--offline"]
        if saved_local_mode:
            help_text = run([exe, "setup", "--help"]).stdout
            assert "--edit" in help_text and "--local" in help_text
            for flag in ("--edit", "--local"):
                unused = root / ("Redirected " + flag.removeprefix("--"))
                blocked = run([exe, "--home", unused, "setup", flag], success=False)
                assert blocked.returncode and not unused.exists()
                assert "attended Windows terminal" in blocked.stdout + blocked.stderr
            preferences = home / "state/startup-preferences.json"
            preferences.parent.mkdir(parents=True, exist_ok=True)
            preferences.write_text(
                json.dumps({"schema": 1, "mode": "offline"}), encoding="utf-8"
            )
            mode_flags = []
        try:
            started = json.loads(
                run(
                    [
                        exe,
                        "--home",
                        home,
                        "start",
                        *mode_flags,
                        "--detach",
                        "--wait",
                        "60",
                        "--json",
                    ]
                ).stdout
            )
            assert (
                started["manager_running"] and started["connection_mode"] == "offline"
            )
        finally:
            stopped = json.loads(
                run([exe, "--home", home, "stop", "--wait", "60", "--json"]).stdout
            )
            assert not stopped["manager_running"]
        assert not (home / "state/computer-access.dpapi").exists()
        assert not (home / "state/connection.json").exists()
        report = dict(
            result="PASS",
            release=release["tag_name"],
            commit=release["target_commitish"],
            archive_sha256=archive_sha,
            public_https_installer=True,
            public_https_archive=True,
            python_on_child_path=False,
            node_on_child_path=False,
            embedded_runtime=True,
            bundled_helpers=True,
            office_modules=True,
            command_launcher=True,
            mcp_config=True,
            offline_start_stop=True,
            redirected_setup_refused=True,
            saved_local_mode_reused=saved_local_mode,
            setup_edit_local_cli=saved_local_mode,
            owner_access_not_granted=True,
            user_path_not_changed=True,
        )
    # Report success only after the disposable installation has been removed.
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
