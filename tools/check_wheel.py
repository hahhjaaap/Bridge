"""Install the built wheel in a fresh interpreter and run outside the checkout."""

import argparse
import json
import os
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        required = {
            "udb/__main__.py",
            "udb/cli.py",
            "udb/browser_files.py",
            "udb/desktop_control.py",
            "udb/windows_utilities.py",
            "udb/windows_utilities.ps1",
            "udb/companion/runtime.py",
            "udb/companion/udb_pointer_overlay.ps1",
            "udb/computer_uia.ps1",
            "udb/desktop_guest.py",
            "udb/plugin.template.json",
            "udb_companion.py",
            "udb_extensions.py",
            "udb_features.py",
            "udb_managed_process.py",
            "udb_recovery.py",
            "udb_system_processes.py",
            "udb_tool_catalog.py",
            "udb_virtual_pointer.py",
            "udb_work_journal.py",
        }
        if not required <= names:
            raise RuntimeError("Wheel misses runtime assets: " + str(required - names))
        if any(
            "/state/" in n or "/.udb-work-state/" in n or n.endswith((".exe", ".dpapi"))
            for n in names
        ):
            raise RuntimeError("Private/runtime artifact in wheel")
    with tempfile.TemporaryDirectory(prefix="udb-wheel-") as temp:
        root = Path(temp)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "PYTHONPATH",
                "UDB_HOME",
                "CONTROL_PLANE_API_KEY",
                "TUNNEL_CLIENT_PROFILE_DIR",
            )
        }
        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheel)],
            env=env,
            check=True,
            cwd=root,
            stdout=subprocess.DEVNULL,
        )
        probe = "import json,udb,udb_companion;from udb.companion import runtime;assert udb_companion is runtime;print(json.dumps({'version':udb.__version__,'file':udb.__file__}))"
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            env=env,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        info = json.loads(result.stdout)
        if not Path(info["file"]).is_relative_to(environment):
            raise RuntimeError("Test resolved source checkout instead of wheel")
        result = subprocess.run(
            [str(python), "-I", "-m", "udb", "tools"],
            env=env,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        data = json.loads(result.stdout)
        if data["version"] != info["version"] or data["tool_count"] != 61:
            raise RuntimeError("Installed wheel has wrong catalog/version")
        print(
            json.dumps(
                dict(
                    result="PASS",
                    version=info["version"],
                    tools=data["tool_count"],
                    isolated=True,
                    external_core_exe_required=False,
                )
            )
        )


if __name__ == "__main__":
    main()
