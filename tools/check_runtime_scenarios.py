"""Windows scenario acceptance using owned fixtures and existing local access."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from udb.core import Agent
from udb.host_client import command as host_command
from udb.launcher import child_environment, worker_command


def browser_fixture():
    with tempfile.TemporaryDirectory(prefix="udb-hostqa-") as directory:
        home = Path(directory).resolve()
        (home / "QA-ISOLATED").write_text("Disposable browser fixture")
        with (home / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                worker_command("core", ["--home", str(home), "--host-engine-qa"]),
                env=child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                ready = home / "fixture-ready.json"
                deadline = time.monotonic() + 75
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        details = (home / "worker.log").read_text(
                            encoding="utf-8", errors="replace"
                        )[-6000:]
                        raise RuntimeError(
                            "Disposable browser fixture did not start "
                            f"(exit={process.poll()}):\n{details}"
                        )
                    time.sleep(0.1)
                issued = json.loads(ready.read_text())
                agent = Agent(home)
                args = dict(session_id=issued["session_id"], tab_id=issued["tab_id"])
                row = host_command(agent, "browser", action="read", **args)
                assert row["title"] == "UDB owned fixture"
                value = "UDB Unicode kiểm thử"
                assert host_command(
                    agent,
                    "browser",
                    action="fill",
                    selector="#name",
                    value=value,
                    **args,
                )["ok"]
                assert host_command(
                    agent, "browser", action="click", selector="#confirm", **args
                )["ok"]
                assert (
                    value
                    in host_command(agent, "browser", action="read", **args)["text"]
                )
                image = host_command(agent, "browser", action="screenshot", **args)
                assert base64.b64decode(image["png_base64"]).startswith(
                    b"\x89PNG\r\n\x1a\n"
                )
                assert host_command(agent, "close", session_id=args["session_id"])[
                    "closed"
                ]
                assert not (agent.state / "computer-access.dpapi").exists()
            finally:
                (home / "stop.flag").write_text("stop owned test fixture")
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
            assert process.returncode == 0
    return dict(
        browser="PASS", read_fill_click_screenshot=True, permissions_issued=False
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wheel",
        action="store_true",
        help="Also install a wheel in a fresh temporary venv",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    logs = root / "reports" / "runtime-scenarios"
    logs.mkdir(parents=True, exist_ok=True)
    results = []

    def run(name, argv, timeout=180):
        print(json.dumps(dict(scenario=name, state="RUNNING")), flush=True)
        with (logs / (name + ".log")).open("wb") as output:
            result = subprocess.run(
                argv,
                cwd=root,
                env=child_environment(),
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        if result.returncode:
            details = (logs / (name + ".log")).read_text(
                encoding="utf-8", errors="replace"
            )
            raise RuntimeError(name + " failed:\n" + details[-6000:])
        results.append(dict(scenario=name, state="PASS"))
        print(json.dumps(results[-1]), flush=True)

    for name in (
        "test_backend_recovery_mcp",
        "test_documents_mcp",
        "test_uia_provider",
    ):
        run(name, [sys.executable, str(root / "tests/components" / (name + ".py"))])
    print(json.dumps(dict(scenario="owned_browser", state="RUNNING")), flush=True)
    results.append(browser_fixture())
    print(json.dumps(results[-1]), flush=True)
    helper = root / "build-assets/bin/tunnel-client.exe"
    help_result = subprocess.run(
        [str(helper), "run", "--help"],
        env=child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0
    assert (
        "mcp.stdio-send-initialized-notification"
        in help_result.stdout + help_result.stderr
    )
    results.append(dict(scenario="tunnel_initialized_option", state="PASS"))
    if args.wheel:
        destination = root / "build" / "runtime-wheel"
        run(
            "build_wheel",
            [
                sys.executable,
                "-I",
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(destination),
            ],
        )
        wheels = list(destination.glob("*.whl"))
        assert len(wheels) == 1
        run(
            "isolated_wheel",
            [sys.executable, "-I", str(root / "tools/check_wheel.py"), str(wheels[0])],
            timeout=360,
        )
    report = dict(
        result="PASS",
        scenarios=results,
        physical_mouse_keyboard_tested=False,
        sandbox_guest_boot_tested=False,
    )
    (logs / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
