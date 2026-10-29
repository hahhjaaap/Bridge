"""Crash only children of an owned real proxy; verify subsequent MCP calls."""

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

from test_uia_provider import Client, object_data


def terminate_backend(proxy_pid, kind):
    powershell = str(
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    # A Windows venv launcher can insert a base-interpreter child for both
    # proxy and worker. Inspect argv only inside this fixture's owned tree.
    script = """$all=@(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,ExecutablePath,CreationDate)
$ids=@([uint32]PROXY_PID)
for($i=0;$i -lt 8;$i++){$new=@($all|Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids});if(!$new.Count){break};$ids+=@($new.ProcessId)}
$all|Where-Object {$_.ProcessId -in $ids -and $_.ProcessId -ne PROXY_PID}|ForEach-Object {
 $cmd=(Get-CimInstance Win32_Process -Filter ('ProcessId='+$_.ProcessId) -Property CommandLine).CommandLine
 @{pid=$_.ProcessId;parent=$_.ParentProcessId;image=$_.ExecutablePath;creation=$_.CreationDate.ToFileTimeUtc();role=$(if($cmd -match '--stdio'){'original'}elseif($cmd -match 'udb_companion.py|_worker\\s+companion'){'companion'}else{'unknown'})}
}|ConvertTo-Json -Compress
""".replace("PROXY_PID", str(proxy_pid))
    rows = json.loads(
        subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
    )
    if isinstance(rows, dict):
        rows = [rows]
    selected = [r for r in rows if r["role"] == kind]
    selected = [
        r for r in selected if not any(c["parent"] == r["pid"] for c in selected)
    ]
    assert len(selected) == 1, "Not one test-owned backend"
    row = selected[0]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE,
        w.DWORD,
        w.LPWSTR,
        ctypes.POINTER(w.DWORD),
    ]
    k.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.c_void_p] * 4)]
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.OpenProcess(0x100000 | 0x1000 | 1, False, row["pid"])
    assert handle
    try:
        times = (ctypes.c_uint64 * 4)()
        assert k.GetProcessTimes(
            handle, *[ctypes.byref(times, i * 8) for i in range(4)]
        )
        assert abs(int(times[0]) - row["creation"]) <= 10000, "PID identity changed"
        image = ctypes.create_unicode_buffer(32768)
        length = w.DWORD(len(image))
        assert k.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length))
        assert Path(image.value) == Path(row["image"])
        assert k.TerminateProcess(handle, 12)
        assert k.WaitForSingleObject(handle, 5000) == 0
    finally:
        k.CloseHandle(handle)


def main():
    if os.name != "nt":
        raise RuntimeError("Windows required")
    client = Client()

    def tool(name, **args):
        return client.rpc("tools/call", dict(name=name, arguments=args))

    def recovered_read(name):
        assert name in {"bridge_status", "terminal_list"}
        deadline = time.monotonic() + 10
        while True:
            row = client.rpc(
                "tools/call", dict(name=name, arguments={}), allow_error=True
            )
            if "error" not in row:
                return row
            error = row["error"]
            # The pipe may close just after dispatch. That call must fail
            # without replay; issue a separate read-only probe afterwards.
            assert error["code"] == -32000 and "disconnected" in error["message"], row
            assert error["data"]["udb"]["replay_performed"] is False, row
            assert time.monotonic() < deadline, row
            time.sleep(0.1)

    try:
        tools = client.rpc("tools/list", {})["tools"]
        assert len(tools) == 59
        assert not tool("bridge_status").get("isError")
        terminate_backend(client.p.pid, "original")
        assert not recovered_read("bridge_status").get("isError"), (
            "Real original backend did not recover"
        )
        start = tool(
            "terminal_start",
            program=sys.executable,
            arguments=[
                "-u",
                "-c",
                "import os,time;print('TEST_PID',os.getpid(),flush=True);time.sleep(90)",
            ],
            wait_ms=200,
        )
        raw = "\n".join(b.get("text", "") for b in start.get("content", []))
        sid = re.search(r"Phiên: ([a-f0-9]{12})", raw).group(1)
        for _ in range(50):
            match = re.search(r"TEST_PID (\d+)", raw)
            if match:
                break
            time.sleep(0.1)
            raw += "\n" + "\n".join(
                b.get("text", "")
                for b in tool(
                    "terminal_read", session_id=sid, offset=0, wait_ms=200
                ).get("content", [])
            )
        assert match, "No test terminal PID: " + raw
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.CloseHandle.argtypes = [w.HANDLE]
        process = k.OpenProcess(0x100000, False, int(match.group(1)))
        assert process
        try:
            terminate_backend(client.p.pid, "companion")
            assert k.WaitForSingleObject(process, 5000) == 0, (
                "Kernel did not stop fixture terminal tree"
            )
        finally:
            k.CloseHandle(process)
        sessions = recovered_read("terminal_list")
        assert not sessions.get("isError")
        assert "No companion-owned terminal sessions" in json.dumps(sessions)
        assert tool("terminal_read", session_id=sid).get("isError"), (
            "Lost session must not be replayed/reattached"
        )
        status = tool("bridge_status")
        health = None
        for b in status["content"]:
            try:
                value = json.loads(b.get("text", ""))
                if "backend_health" in value:
                    health = value["backend_health"]
            except ValueError:
                pass
        assert (
            health
            and health["original"]["generation"] >= 2
            and health["companion"]["generation"] >= 2
        ), health
        assert (
            not health["original"]["replay_enabled"]
            and not health["companion"]["replay_enabled"]
        )
        print(
            "PASS | Real proxy recovers original and Companion backends; kernel stops lost terminal, old ID refused, no session replay"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
