"""Windows job containment for processes spawned and owned by UDB.

No dependency on browser automation or workspace UI code.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from functools import wraps

from .resources import LIMITS


def _job_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._job_lock:
            return method(self, *args, **kwargs)

    return wrapped


class OwnedProcess:
    """Windows Job Object: membership is captured at spawn, never trusted PIDs."""

    def __init__(self, argv, cwd, memory_mib=0):
        self._job_lock = threading.RLock()
        import win32api
        import win32con
        import win32job
        import win32process

        self.job = win32job.CreateJobObject(None, "Local\\UDB-" + uuid.uuid4().hex)
        self.handle = None
        try:
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            info["BasicLimitInformation"]["ActiveProcessLimit"] = (
                LIMITS["max_browser_processes"] if memory_mib else 2
            )
            if memory_mib:
                info["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
                )
                info["JobMemoryLimit"] = memory_mib * 1048576
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, info
            )
            if memory_mib:
                # CPU hard cap applies to the entire owned browser process tree.
                import ctypes
                from ctypes import wintypes as w

                class CpuRate(ctypes.Structure):
                    _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

                cpu = CpuRate(1 | 4, 2500)  # ENABLE | HARD_CAP, 25% of machine CPU
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.SetInformationJobObject.argtypes = [
                    w.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    w.DWORD,
                ]
                if not k.SetInformationJobObject(
                    int(self.job), 15, ctypes.byref(cpu), ctypes.sizeof(cpu)
                ):
                    raise OSError(
                        ctypes.get_last_error(), "Browser CPU cap unavailable"
                    )
            flags = (
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW | 0x00004000
            )  # BELOW_NORMAL_PRIORITY_CLASS
            self.handle, thread, self.pid, _ = win32process.CreateProcess(
                str(argv[0]),
                subprocess.list2cmdline(list(map(str, argv))),
                None,
                None,
                False,
                flags,
                None,
                str(cwd),
                win32process.STARTUPINFO(),
            )
            try:
                win32job.AssignProcessToJobObject(self.job, self.handle)
                win32process.ResumeThread(thread)
            except BaseException:
                win32api.TerminateProcess(self.handle, 1)
                raise
            finally:
                thread.Close()
        except BaseException:
            self.close()
            raise

    @_job_locked
    def alive(self):
        if not self.handle:
            return False
        import win32process

        return win32process.GetExitCodeProcess(self.handle) == 259

    @_job_locked
    def usage(self):
        import win32job

        if not self.job:
            return dict(processes=0, peak_commit_mib=0)
        data = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation
        )
        memory = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation
        )
        cpu_cap = None
        if memory["JobMemoryLimit"]:
            import ctypes
            from ctypes import wintypes as w

            class CpuRate(ctypes.Structure):
                _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

            value = CpuRate()
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.QueryInformationJobObject.argtypes = [
                w.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                w.DWORD,
                ctypes.c_void_p,
            ]
            if (
                k.QueryInformationJobObject(
                    int(self.job), 15, ctypes.byref(value), ctypes.sizeof(value), None
                )
                and value.flags & 4
            ):
                cpu_cap = value.rate / 100
        return dict(
            processes=data["ActiveProcesses"],
            peak_commit_mib=round(memory["PeakJobMemoryUsed"] / 1048576, 1),
            memory_limit_mib=round(memory["JobMemoryLimit"] / 1048576),
            cpu_cap_percent=cpu_cap,
        )

    @_job_locked
    def owns_pid(self, pid):
        import win32api
        import win32job

        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                return bool(self.job and win32job.IsProcessInJob(handle, self.job))
            finally:
                handle.Close()
        except Exception:
            return False

    @_job_locked
    def close(self):
        # Closing this private job terminates only members created by us.
        if getattr(self, "job", None):
            self.job.Close()
            self.job = None
        if getattr(self, "handle", None):
            import win32event

            win32event.WaitForSingleObject(self.handle, 3000)
            self.handle.Close()
            self.handle = None
