"""Read system process metadata; terminate only a freshly observed same-user process.

No command lines, environment, privileges, debug rights or arbitrary PID killing.
"""

from __future__ import annotations

import ctypes
import os
import signal
import time
import uuid
from pathlib import Path


class ProcessCatalog:
    def __init__(self, project_root, bridge_home=None):
        root = Path(project_root)
        self.install = (
            Path(bridge_home).resolve()
            if bridge_home
            else root.parent.parent
            if root.parent.name.lower() == "projects"
            else None
        )
        self.observations = {}
        self.cpu = {}

    def _windows(self):
        from ctypes import wintypes as w

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)

        def api(lib, n, r, *args):
            f = getattr(lib, n)
            f.restype = r
            f.argtypes = list(args)
            return f

        api(k, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        api(k, "CloseHandle", w.BOOL, w.HANDLE)
        api(k, "GetCurrentProcess", w.HANDLE)
        api(k, "GetProcessTimes", w.BOOL, w.HANDLE, *([ctypes.c_void_p] * 4))
        api(
            k,
            "QueryFullProcessImageNameW",
            w.BOOL,
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            ctypes.POINTER(w.DWORD),
        )
        api(k, "IsProcessCritical", w.BOOL, w.HANDLE, ctypes.POINTER(w.BOOL))
        api(a, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        api(
            a,
            "GetTokenInformation",
            w.BOOL,
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        )
        api(a, "GetLengthSid", w.DWORD, ctypes.c_void_p)

        def sid(handle):
            token = w.HANDLE()
            if not a.OpenProcessToken(handle, 8, ctypes.byref(token)):
                return None
            try:
                size = w.DWORD()
                a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
                if not size.value:
                    return None
                buf = ctypes.create_string_buffer(size.value)
                if not a.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                    return None
                ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                return ctypes.string_at(ptr, a.GetLengthSid(ptr))
            finally:
                k.CloseHandle(token)

        owner = sid(k.GetCurrentProcess())
        self.k = k
        self.sid = sid
        self.owner = owner

        class Entry(ctypes.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("usage", w.DWORD),
                ("pid", w.DWORD),
                ("heap", ctypes.c_size_t),
                ("module", w.DWORD),
                ("threads", w.DWORD),
                ("parent", w.DWORD),
                ("priority", w.LONG),
                ("flags", w.DWORD),
                ("name", w.WCHAR * 260),
            ]

        api(k, "CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
        api(k, "Process32FirstW", w.BOOL, w.HANDLE, ctypes.POINTER(Entry))
        api(k, "Process32NextW", w.BOOL, w.HANDLE, ctypes.POINTER(Entry))
        snap = k.CreateToolhelp32Snapshot(2, 0)
        if snap == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        rows = []
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            ok = k.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                rows.append(
                    {
                        "pid": int(entry.pid),
                        "parent_pid": int(entry.parent),
                        "name": entry.name,
                    }
                )
                ok = k.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k.CloseHandle(snap)

        class Memory(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [
                (n, ctypes.c_size_t)
                for n in (
                    "peak",
                    "working",
                    "ppeak",
                    "paged",
                    "npeak",
                    "nonpaged",
                    "pagefile",
                    "pagepeak",
                    "private",
                )
            ]

        ps = ctypes.WinDLL("psapi", use_last_error=True)
        api(
            ps,
            "GetProcessMemoryInfo",
            w.BOOL,
            w.HANDLE,
            ctypes.POINTER(Memory),
            w.DWORD,
        )
        for row in rows:
            h = k.OpenProcess(0x1000, False, row["pid"])
            row.update(
                creation=None,
                same_user=False,
                critical=True,
                image="",
                cpu_seconds=None,
                working_set_mib=None,
                private_commit_mib=None,
            )
            if not h:
                continue
            try:
                times = (ctypes.c_uint64 * 4)()
                if k.GetProcessTimes(
                    h, *[ctypes.byref(times, i * 8) for i in range(4)]
                ):
                    row["creation"] = int(times[0])
                    row["cpu_seconds"] = (times[2] + times[3]) / 10000000
                row["same_user"] = bool(owner and sid(h) == owner)
                critical = w.BOOL(True)
                if k.IsProcessCritical(h, ctypes.byref(critical)):
                    row["critical"] = bool(critical.value)
                size = w.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    row["image"] = buf.value
                mem = Memory()
                mem.cb = ctypes.sizeof(mem)
                if ps.GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb):
                    row["working_set_mib"] = round(mem.working / 1048576, 2)
                    row["private_commit_mib"] = round(mem.private / 1048576, 2)
            finally:
                k.CloseHandle(h)
        return rows

    def _linux(self):
        if int(Path("/proc/self/stat").read_text().split(" ", 1)[0]) != os.getpid():
            raise RuntimeError(
                "Process catalog unavailable: procfs uses a different PID namespace"
            )
        rows = []
        for folder in Path("/proc").iterdir():
            if not folder.name.isdigit():
                continue
            try:
                raw = (folder / "stat").read_text()
                tail = raw.rsplit(")", 1)[1].split()
                rows.append(
                    dict(
                        pid=int(folder.name),
                        parent_pid=int(tail[1]),
                        name=(folder / "comm").read_text().strip(),
                        creation=int(tail[19]),
                        same_user=folder.stat().st_uid == os.getuid(),
                        critical=int(folder.name) < 5,
                        image=os.readlink(folder / "exe"),
                        cpu_seconds=(int(tail[11]) + int(tail[12]))
                        / os.sysconf("SC_CLK_TCK"),
                        working_set_mib=round(
                            int(tail[21]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 2
                        ),
                        private_commit_mib=None,
                    )
                )
            except (OSError, ValueError, IndexError):
                continue
        return rows

    def list(self, match="", offset=0, limit=100):
        if not isinstance(match, str) or len(match) > 200:
            raise ValueError("Process-name filter limit: 200 characters")
        if not 0 <= int(offset) <= 10000 or not 1 <= int(limit) <= 200:
            raise ValueError("Process offset/limit out of range")
        rows = self._windows() if os.name == "nt" else self._linux()
        ancestors = {os.getpid()}
        parents = {r["pid"]: r["parent_pid"] for r in rows}
        pid = os.getpid()
        for _ in range(64):
            pid = parents.get(pid, 0)
            if not pid or pid in ancestors:
                break
            ancestors.add(pid)
        now = time.monotonic()
        self.observations = {
            k: v for k, v in self.observations.items() if v["until"] > now
        }
        selected = []
        for row in sorted(rows, key=lambda r: r["pid"]):
            if match.casefold() not in row["name"].casefold():
                continue
            same_install = bool(
                self.install
                and row["image"]
                and Path(row["image"]).is_relative_to(self.install)
            )
            allowed = bool(
                row["same_user"]
                and not row["critical"]
                and row["pid"] not in ancestors
                and not same_install
                and row["creation"] is not None
                and row["image"]
            )
            key = (row["pid"], row["creation"])
            old = self.cpu.get(key)
            seconds = row["cpu_seconds"]
            row["cpu_percent"] = None
            if old and seconds is not None and now > old[0]:
                row["cpu_percent"] = round(
                    max(
                        0,
                        100
                        * (seconds - old[1])
                        / (now - old[0])
                        / max(1, os.cpu_count() or 1),
                    ),
                    2,
                )
            if seconds is not None:
                self.cpu[key] = (now, seconds)
            row.pop("image", None)
            row.pop("critical", None)
            row.pop("creation", None)
            row["can_terminate"] = allowed
            if allowed and int(offset) <= len(selected) < int(offset) + int(limit):
                token = uuid.uuid4().hex
                if len(self.observations) >= 2000:
                    self.observations.pop(next(iter(self.observations)))
                self.observations[token] = dict(
                    pid=key[0], creation=key[1], until=now + 60
                )
                row["observation_id"] = token
            selected.append(row)
        self.cpu = {k: v for k, v in self.cpu.items() if now - v[0] < 120}
        return {
            "processes": selected[int(offset) : int(offset) + int(limit)],
            "total": len(selected),
            "next_offset": int(offset) + int(limit)
            if int(offset) + int(limit) < len(selected)
            else None,
            "observation_expires_seconds": 60,
            "command_lines_included": False,
        }

    def terminate(self, observation_id):
        row = self.observations.pop(observation_id, None)
        if not row or time.monotonic() > row["until"]:
            raise ValueError("Observation expired; list processes again")
        pid = row["pid"]
        if os.name == "nt":
            from ctypes import wintypes as w

            h = self.k.OpenProcess(0x1001, False, pid)
            if not h:
                raise PermissionError(
                    "Windows refused query/termination; no elevation attempted"
                )
            try:
                times = (ctypes.c_uint64 * 4)()
                critical = w.BOOL(True)
                if (
                    not self.k.GetProcessTimes(
                        h, *[ctypes.byref(times, i * 8) for i in range(4)]
                    )
                    or times[0] != row["creation"]
                ):
                    raise ValueError("Process exited or PID was reused")
                if (
                    self.sid(h) != self.owner
                    or not self.k.IsProcessCritical(h, ctypes.byref(critical))
                    or critical.value
                ):
                    raise PermissionError("Process owner or critical status changed")
                self.k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
                self.k.TerminateProcess.restype = w.BOOL
                if not self.k.TerminateProcess(h, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.k.CloseHandle(h)
        else:
            if not hasattr(os, "pidfd_open") or not hasattr(
                signal, "pidfd_send_signal"
            ):
                raise RuntimeError("Safe process termination unavailable")
            fd = os.pidfd_open(pid)
            try:
                stat = Path("/proc") / str(pid)
                creation = int(
                    (stat / "stat").read_text().rsplit(")", 1)[1].split()[19]
                )
                if creation != row["creation"] or stat.stat().st_uid != os.getuid():
                    raise ValueError("Process identity changed")
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        return {
            "pid": pid,
            "termination_requested": True,
            "elevated": False,
            "replay_safe": False,
        }
