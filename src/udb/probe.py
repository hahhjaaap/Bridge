"""Fixed local MCP acceptance, using the installed agent and existing grants.

No Desktop Commander, cloud API, credentials, permission setters, arbitrary
commands or supplied test paths. Only disposable test files and an owned window.
This cannot certify the tool catalog cached by a ChatGPT conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path

TEXT = "UDB độc lập — tiếng Việt 🦈"
REQUIRED = {
    "bridge_status",
    "computer_status",
    "computer_files",
    "computer_execute",
    "computer_process",
    "computer_desktop",
    "host_status",
    "host_start_session",
    "host_workspace",
    "host_close_session",
}


class ProbeFailure(Exception):
    pass


def safe_error(exc):
    if isinstance(exc, ProbeFailure):
        return str(exc)
    for child in getattr(exc, "exceptions", ()):
        detail = safe_error(child)
        if detail:
            return detail
    return None


def payload(result, tool):
    if result.isError:
        raise ProbeFailure(
            tool + " was refused; inspect udb doctor and resource/access status."
        )
    data = result.structuredContent
    if data is None:
        text = next(
            (block.text for block in result.content if block.type == "text"), ""
        )
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            raise ProbeFailure(tool + " returned invalid structured data.") from None
    if not isinstance(data, dict):
        raise ProbeFailure(tool + " returned an unexpected result.")
    return data


async def exercise(session, folder, powershell, report):
    """Fixed requests; injectable session supports protocol/cleanup regression tests."""

    async def call(name, **args):
        result = await asyncio.wait_for(session.call_tool(name, args), 25)
        return payload(result, name)

    names = {t.name for t in (await asyncio.wait_for(session.list_tools(), 20)).tools}
    report["mcp_tool_count"] = len(names)
    if not REQUIRED <= names:
        raise ProbeFailure(
            "Installed EXE lacks required tools; update the EXE and companion together."
        )
    status = await call("bridge_status")
    report.update(
        version=status.get("version"),
        manager_mode=status.get("manager", {}).get("mode"),
        worker_ready=status.get("host_background", {}).get("worker_ready", False),
    )
    if not report["worker_ready"]:
        raise ProbeFailure("UDB worker is unavailable. Run udb first.")
    report["local_mcp"] = "PASS"
    access = await call("computer_status")
    report["computer_access_enabled"] = access.get("enabled") is True
    if not report["computer_access_enabled"]:
        report.update(
            result="NEEDS_OWNER_ACCESS",
            next_action="Run udb --access computer in your own terminal. No permission was created.",
        )
        return
    sid = None
    job = None
    try:
        file = str(folder / "unicode.txt")
        written = await call(
            "computer_files",
            action="write",
            path=file,
            content=TEXT,
            expected_sha256="NEW",
        )
        read = await call("computer_files", action="read", path=file, limit=512)
        if read.get("content") != TEXT or read.get("sha256") != written.get("sha256"):
            raise ProbeFailure("Computer file readback mismatch.")
        report["file_roundtrip"] = "PASS"
        script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);[IO.File]::WriteAllText((Join-Path (Get-Location) 'command.txt'),'UDB_COMMAND_OK',[Text.UTF8Encoding]::new($false));Write-Output 'UDB_COMMAND_OK'"
        report["stage"] = "command_start"
        started = await call(
            "computer_execute",
            program=str(powershell),
            arguments=["-NoProfile", "-NonInteractive", "-Command", script],
            cwd=str(folder),
            timeout_seconds=30,
        )
        job = started["job_id"]
        until = time.monotonic() + 35
        report["stage"] = "command_poll"
        while True:
            output = await call("computer_process", action="read", job_id=job)
            if output.get("status") != "running":
                break
            if time.monotonic() > until:
                raise ProbeFailure(
                    "Test command timed out; its owned job will be stopped."
                )
            await asyncio.sleep(0.2)
        if (
            output.get("status") != "exited"
            or output.get("exit_code") != 0
            or "UDB_COMMAND_OK" not in output.get("output", "")
        ):
            raise ProbeFailure("Test command did not finish successfully.")
        report["stage"] = "command_readback"
        generated = await call(
            "computer_files", action="read", path=str(folder / "command.txt"), limit=512
        )
        if generated.get("content") != "UDB_COMMAND_OK":
            raise ProbeFailure("Generated file readback mismatch.")
        report["command_roundtrip"] = "PASS"
        report["stage"] = "uia"
        host = await call("host_status")
        if not host.get("enabled"):
            report["uia_roundtrip"] = "SKIPPED_HOST_MODE_DISABLED"
        elif any(s.get("kind") == "workspace" for s in host.get("sessions", [])):
            report["uia_roundtrip"] = "SKIPPED_EXISTING_WORKSPACE"
        else:
            created = await call(
                "host_start_session",
                kind="workspace",
                duration_seconds=120,
                task="Fixed UDB independent self-test on its own disposable native text window; no user files or input.",
            )
            sid = created["session_id"]
            # Discard every unrelated window title; only our nonce-matched owned
            # workspace is ever inspected/edited or included in the report.
            observed = await call("computer_desktop", action="list")
            expected = "UDB Workspace · " + sid[:8]
            matches = [
                w for w in observed.get("windows", []) if w.get("title") == expected
            ]
            if len(matches) != 1:
                raise ProbeFailure(
                    "Owned test window was not uniquely observed; no desktop input sent."
                )
            wid = matches[0]["window_id"]
            tree = await call("computer_desktop", action="snapshot", window_id=wid)
            if not any(
                e.get("automation_id") == "1001" for e in tree.get("elements", [])
            ):
                raise ProbeFailure("Owned test control was not observed.")
            value = await call(
                "computer_desktop",
                action="set_value",
                window_id=wid,
                automation_id="1001",
                text=TEXT,
            )
            readback = await call("host_workspace", session_id=sid, action="read")
            if not value.get("verified_control_value") or readback.get("text") != TEXT:
                raise ProbeFailure("UI Automation readback mismatch.")
            report["uia_roundtrip"] = "PASS"
        report["result"] = "PASS" if report["uia_roundtrip"] == "PASS" else "PARTIAL"
        report["stage"] = "complete"
    finally:
        # Never close an existing session or an arbitrary process. Cleanup does
        # not replay failed writes or a command whose outcome is unknown.
        failed = []
        if job:
            try:
                await call("computer_process", action="stop", job_id=job)
            except Exception:
                failed.append("owned_command")
        if sid:
            try:
                await call("host_close_session", session_id=sid)
            except Exception:
                failed.append("owned_workspace")
        report["cleanup"] = "PASS" if not failed else "FAILED"
        if failed:
            raise ProbeFailure(
                "Owned fixture cleanup failed; stop udb before retrying."
            )


async def run(home):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    report = dict(
        result="FAIL",
        transport="local_exe_stdio",
        chatgpt_tool_discovery_verified=False,
        mouse_keyboard_tested=False,
        permission_created=False,
    )
    if os.name != "nt":
        return dict(**report, error="This installed-agent test requires Windows.")
    home = await asyncio.to_thread(Path(home).resolve)
    from .launcher import worker_command
    from .windows_paths import system_executable

    argv = worker_command("core")
    command = argv[0]
    args = argv[1:]
    args += ["--stdio", "--home", str(home)]
    errors = None
    try:
        errors = await asyncio.to_thread(open, os.devnull, "w")
        with tempfile.TemporaryDirectory(prefix="udb-probe-") as temp:
            parameters = StdioServerParameters(
                command=command, args=args, cwd=str(home)
            )
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=25)
                ) as session:
                    await asyncio.wait_for(session.initialize(), 20)
                    await exercise(
                        session,
                        Path(temp),
                        Path(system_executable("powershell.exe")),
                        report,
                    )
    except Exception as exc:
        # ExceptionGroups may wrap SDK errors. Do not print server/UI content,
        # paths, credential errors or raw subprocess output from them.
        report["result"] = "FAIL"
        report["error"] = (
            safe_error(exc)
            or "Local MCP test failed or timed out. Run udb doctor; inspect access/resources before retrying."
        )
    finally:
        if errors is not None:
            errors.close()
    return report
