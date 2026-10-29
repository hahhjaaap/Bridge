"""Exercise the standalone CLI and real MCP worker tree without owner grants."""

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def temporary_directory():
    directory = tempfile.TemporaryDirectory(prefix="udb-frozen-")
    try:
        yield directory.name
    finally:
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
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    source_executable = args.executable.resolve()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("_PYI_")
        and k
        not in {
            "_MEIPASS2",
            "CONTROL_PLANE_API_KEY",
            "TUNNEL_CLIENT_PROFILE_DIR",
            "PYTHONPATH",
            "UDB_HOME",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
        }
    }
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        env["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    with (
        temporary_directory() as temporary,
        ExitStack() as cleanup,
    ):
        root = Path(temporary).resolve()
        runtime_temp = root / "runtime-temp"
        runtime_temp.mkdir()
        env["TEMP"] = env["TMP"] = str(runtime_temp)
        portable = root / "copied-app"
        portable.mkdir()
        executable = portable / source_executable.name
        shutil.copyfile(source_executable, executable)
        assert list(portable.iterdir()) == [executable]
        home = root / "home"
        project = root / "project"
        home.mkdir()
        project.mkdir()

        def run(*arguments, input_text=None, expect_success=True, error_contains=None):
            io_args = (
                {"stdin": subprocess.DEVNULL}
                if input_text is None
                else {"input": input_text}
            )
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                **io_args,
            )
            if expect_success:
                assert result.returncode == 0, result.stderr + result.stdout
                assert "Failed to remove temporary directory" not in result.stderr, (
                    result.stderr
                )
            else:
                assert result.returncode != 0, (
                    "A noninteractive permission request succeeded"
                )
            if error_contains is not None:
                assert error_contains in result.stdout + result.stderr
            return result.stdout

        version = run("--version").strip()
        remembered_mode = tuple(map(int, version.split("."))) >= (0, 9, 3)
        if remembered_mode:
            help_text = run("setup", "--help")
            assert "--edit" in help_text and "--local" in help_text
            for flag in ("--edit", "--local"):
                refused = root / ("redirected-" + flag.removeprefix("--"))
                run(
                    "setup",
                    "--home",
                    str(refused),
                    flag,
                    expect_success=False,
                    error_contains="attended Windows terminal",
                )
                assert not refused.exists()
        catalog = json.loads(run("tools"))
        manifest = json.loads(run("manifest"))
        info = json.loads(run("build-info"))
        assert catalog["version"] == manifest["version"] == info["version"] == version
        assert catalog["tool_count"] == 59 and catalog["backend_count"] == 67
        if os.name == "nt":
            assert info["kind"] == "frozen" and info["release_provenance"]
        runtime = json.loads(run("runtime", "--probe"))
        if os.name == "nt":
            assert runtime["python_embedded"] is True
            assert runtime["source_checkout_required"] is False
        assert runtime["desktop_commander_required"] is False
        assert all(item["ok"] for item in runtime.get("component_probes", {}).values())
        assert all(runtime["office_modules"].values())

        # Real parser workers run from the copied EXE, without a Python PATH.
        office_cases = (
            ("spreadsheet", ".xlsx", {"sheets": {"Probe": [["value", 7]]}}),
            ("word_document", ".docx", {"text": "standalone document"}),
            ("pdf_document", ".pdf", {"text": "standalone PDF"}),
        )
        for format_name, extension, values in office_cases:
            stage = project / (".udb-stage-office-frozen-" + format_name + extension)
            stage.write_bytes(b"")
            request = {
                "root": str(project),
                "format": format_name,
                "arguments": {
                    "action": "write",
                    "path": "result" + extension,
                    **values,
                },
                "inputs": {},
                "staging": str(stage),
            }
            output = json.loads(
                run("_worker", "document", input_text=json.dumps(request) + "\n")
            )
            assert "error" not in output and output["format"] == format_name, output
            assert stage.stat().st_size > 100
            assert output["sha256"] == hashlib.sha256(stage.read_bytes()).hexdigest()

        offline_home = root / "offline-home"
        offline_home.mkdir()
        legacy_metadata = offline_home / "installation.json"
        legacy_data = json.dumps(
            {"active": "missing-legacy-release", "sha256": "0" * 64}
        )
        legacy_metadata.write_text(legacy_data, encoding="utf-8")
        offline_flags = ["--offline"]
        if remembered_mode:
            preference = offline_home / "state/startup-preferences.json"
            preference.parent.mkdir()
            preference.write_text(
                json.dumps({"schema": 1, "mode": "offline"}), encoding="utf-8"
            )
            offline_flags = []
        try:
            offline = json.loads(
                run(
                    "start",
                    *offline_flags,
                    "--detach",
                    "--home",
                    str(offline_home),
                    "--json",
                    "--wait",
                    "30",
                )
            )
            assert (
                offline["manager_running"] and offline["connection_mode"] == "offline"
            )
            assert offline["local_tunnel_ready"] is False
            assert legacy_metadata.read_text("utf-8") == legacy_data
            # The start CLI has fully exited. Native modules not yet loaded by
            # the background manager must still exist for later host commands.
            extracts = list(runtime_temp.glob("_MEI*"))
            assert len(extracts) == 1, extracts
            assert list(extracts[0].rglob("win32job*.pyd")), (
                "The launcher removed the live manager's native runtime"
            )
            status = json.loads(run("status", "--home", str(offline_home), "--json"))
            assert status["connection_mode"] == "offline"
            if tuple(map(int, version.split("."))) >= (0, 10, 0):
                doctor = json.loads(
                    run("doctor", "--home", str(offline_home), "--json")
                )
                checks = {row["check"]: row for row in doctor["checks"]}
                assert checks["runtime_integrity"]["ok"] is True
                assert checks["agent_heartbeat"]["ok"] is True
                assert checks["local_tunnel"]["ok"] is None
                assert checks["credentials"]["ok"] is None
                assert all(row["ok"] is not False for row in checks.values())
                assert run("autostart", "--help") and run("update", "--help")
                run(
                    "rollback",
                    "--home",
                    str(offline_home),
                    "--json",
                    expect_success=False,
                    error_contains="one-command installation",
                )
            assert not (offline_home / "state/computer-access.dpapi").exists()
        finally:
            run("stop", "--home", str(offline_home), "--json", "--wait", "30")

        # Bad Office input must fail only that request, leaving MCP alive.
        (project / "broken.xlsx").write_bytes(b"not an Office archive")
        requests = [
            dict(jsonrpc="2.0", id=1, method="initialize", params={}),
            dict(
                jsonrpc="2.0",
                id=2,
                method="tools/call",
                params=dict(
                    name="spreadsheet",
                    arguments=dict(action="inspect", path="broken.xlsx"),
                ),
            ),
            dict(jsonrpc="2.0", id=3, method="ping", params={}),
        ]
        replies = run(
            "_worker",
            "companion",
            "--root",
            str(project),
            input_text="".join(json.dumps(row) + "\n" for row in requests),
        )
        replies = {row["id"]: row for row in map(json.loads, replies.splitlines())}
        assert replies[2]["result"]["isError"] and replies[3]["result"] == {}

        from datetime import timedelta

        import openpyxl

        duration_file = project / "duration.xlsx"
        book = openpyxl.Workbook()
        book.active["A1"] = timedelta(hours=27, minutes=4, seconds=5)
        book.active["A1"].number_format = "[h]:mm:ss"
        book.save(duration_file)
        book.close()
        request = dict(
            root=str(project),
            format="spreadsheet",
            staging=None,
            inputs={
                str(duration_file): hashlib.sha256(
                    duration_file.read_bytes()
                ).hexdigest()
            },
            arguments=dict(action="read", path="duration.xlsx", range="A1:A1"),
        )
        duration = json.loads(
            run("_worker", "document", input_text=json.dumps(request) + "\n")
        )
        assert duration["rows"] == [["1 day, 3:04:05"]]

        unapproved_home = root / "unapproved-home"
        run(
            "allow-source",
            "--home",
            str(unapproved_home),
            "--path",
            str(project),
            expect_success=False,
        )
        assert not unapproved_home.exists()

        # computer_status talks to the local manager; start an ungranted,
        # offline manager before exercising it through the frozen MCP tree.
        run(
            "start",
            "--offline",
            "--detach",
            "--home",
            str(home),
            "--json",
            "--wait",
            "30",
        )
        cleanup.callback(run, "stop", "--home", str(home), "--json", "--wait", "30")
        process = subprocess.Popen(
            [
                str(executable),
                "serve",
                "--home",
                str(home),
                "--root",
                str(project),
                "--enable-terminal",
            ],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        rows = queue.Queue()
        sequence = 0

        def receive():
            for line in iter(process.stdout.readline, b""):
                try:
                    rows.put(json.loads(line))
                except ValueError:
                    rows.put({"invalid_frame": True})

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()

        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            process.stdin.write(
                (
                    json.dumps(
                        dict(jsonrpc="2.0", id=sequence, method=method, params=params)
                    )
                    + "\n"
                ).encode()
            )
            process.stdin.flush()
            deadline = time.monotonic() + 90
            while True:
                row = rows.get(timeout=max(0.01, deadline - time.monotonic()))
                assert not row.get("invalid_frame"), row
                if row.get("id") == sequence:
                    return row

        try:
            initial = rpc(
                "initialize",
                dict(
                    protocolVersion="2024-11-05",
                    capabilities={},
                    clientInfo=dict(name="standalone-acceptance", version="1"),
                ),
            )
            assert initial["result"]["serverInfo"]["version"] == version
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            process.stdin.flush()
            assert len(rpc("tools/list", {})["result"]["tools"]) == 59
            status = rpc("tools/call", dict(name="bridge_status", arguments={}))[
                "result"
            ]
            assert (
                not status.get("isError")
                and status["_meta"]["udb"]["version"] == version
            )
            limits_reply = rpc(
                "tools/call", dict(name="computer_status", arguments={})
            )["result"]
            assert not limits_reply.get("isError"), limits_reply
            limits = limits_reply.get("structuredContent")
            if not isinstance(limits, dict):
                for block in limits_reply["content"]:
                    text = block.get("text", "")
                    start = text.find("{")
                    if block.get("type") == "text" and start >= 0:
                        try:
                            value = json.loads(text[start:])
                        except ValueError:
                            continue
                        if isinstance(value, dict) and "command_limits" in value:
                            limits = value
                            break
            assert isinstance(limits, dict), limits_reply
            assert limits["command_limits"]["max_running"] == 10
            assert limits["command_limits"]["shared_background_limit"] == 10
            denied = rpc(
                "tools/call",
                dict(
                    name="terminal_start",
                    arguments=dict(program=str(executable), arguments=["--version"]),
                ),
            )
            assert "error" in denied or denied.get("result", {}).get("isError"), denied
            missing = rpc(
                "tools/call",
                dict(
                    name="missing_log_fixture",
                    arguments=dict(
                        text="PRIVATE_LOG_PAYLOAD", api_key="PRIVATE_LOG_KEY"
                    ),
                ),
            )
            assert "error" in missing
            logged = run("logs", "--home", str(home), "--once", "--json")
            events = [json.loads(line) for line in logged.splitlines()]
            assert any(
                row["tool"] == "bridge_status" and row["status"] == "PASS"
                for row in events
            )
            assert any(
                row["tool"] == "missing_log_fixture" and row["status"] == "ERROR"
                for row in events
            )
            assert (
                "PRIVATE_LOG_PAYLOAD" not in logged and "PRIVATE_LOG_KEY" not in logged
            )
            assert not (project / ".udb-work-state").exists(), (
                "Denied request created work records"
            )
            assert not (home / "UniversalDesktopBridge.exe").exists()
            print(
                json.dumps(
                    dict(
                        result="PASS",
                        version=version,
                        tools=59,
                        manifest=True,
                        real_mcp_workers=True,
                        owner_access_denied=True,
                        external_core_exe_required=False,
                        copied_executable_only=True,
                        path_without_python=True,
                        office_workers=True,
                        malformed_office_keeps_mcp_alive=True,
                        excel_elapsed_time_json=True,
                        offline_start_stop=True,
                        ignores_missing_legacy_installation=True,
                        live_activity_logs=True,
                        parallel_capacity=10,
                        remembered_local_mode=remembered_mode,
                        bundled_components=list(runtime.get("component_probes", {})),
                    )
                )
            )
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            thread.join(timeout=2)
            process.stdout.close()


if __name__ == "__main__":
    main()
