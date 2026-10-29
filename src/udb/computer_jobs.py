"""Bounded asynchronous commands in Windows Jobs; never attach to caller-supplied PIDs."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .concurrency import MAX_CONCURRENT, TaskPool
from .core import PolicyError, reject_links

MAX_OUTPUT = 4 * 1024 * 1024


def executable_path(target):
    """Execution is already current-user/unsandboxed. Windows system EXEs
    legitimately have WinSxS hard links. Refuse redirection/reparse paths, but
    do not apply the data-file hardlink restriction to loading executable code.
    File APIs keep their original hardlink checks.
    """
    if not target.is_absolute() or not target.is_file():
        raise PolicyError("Executable was not found; provide its absolute path")
    if os.name == "nt" and (str(target).startswith("\\\\") or ":" in str(target)[2:]):
        raise PolicyError(
            "Executable requires a local path without alternate data streams"
        )
    reject_links(target.parent)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PolicyError("Executable symlink/junction/reparse points are not allowed")
    return target


class CommandProcess:
    def __init__(self, argv, cwd, output):
        import _winapi
        import msvcrt

        import win32job
        import win32process

        self.api = _winapi
        self.job = None
        self.handle = None
        self.job = win32job.CreateJobObject(
            None, "Local\\UDB-Command-" + uuid.uuid4().hex
        )
        handles = []
        try:
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            info["BasicLimitInformation"]["ActiveProcessLimit"] = 32
            info["JobMemoryLimit"] = 4096 * 1048576
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, info
            )
            with open(os.devnull, "rb") as incoming, open(output, "xb") as outgoing:
                handles = [
                    msvcrt.get_osfhandle(incoming.fileno()),
                    msvcrt.get_osfhandle(outgoing.fileno()),
                ]
                for h in handles:
                    os.set_handle_inheritable(h, True)
                si = subprocess.STARTUPINFO()
                si.dwFlags = subprocess.STARTF_USESTDHANDLES
                si.hStdInput = handles[0]
                si.hStdOutput = handles[1]
                si.hStdError = handles[1]
                si.lpAttributeList = {"handle_list": handles}
                env = os.environ.copy()
                for key in ("CONTROL_PLANE_API_KEY", "TUNNEL_CLIENT_PROFILE_DIR"):
                    env.pop(key, None)
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                self.handle, thread, self.pid, _ = _winapi.CreateProcess(
                    str(argv[0]),
                    subprocess.list2cmdline(argv),
                    None,
                    None,
                    True,
                    0x4 | 0x08000000 | 0x00004000,
                    env,
                    str(cwd),
                    si,
                )
                try:
                    win32job.AssignProcessToJobObject(self.job, self.handle)
                    win32process.ResumeThread(thread)
                except BaseException:
                    _winapi.TerminateProcess(self.handle, 1)
                    raise
                finally:
                    _winapi.CloseHandle(thread)
                    for h in handles:
                        os.set_handle_inheritable(h, False)
        except BaseException:
            self.close()
            raise

    def poll(self):
        import win32job

        if not self.handle:
            return -1
        info = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation
        )
        if info["ActiveProcesses"]:
            return None
        return self.api.GetExitCodeProcess(self.handle)

    def close(self):
        if self.job:
            self.job.Close()
            self.job = None
        if self.handle:
            self.api.WaitForSingleObject(self.handle, 3000)
            self.api.CloseHandle(self.handle)
            self.handle = None


class Jobs:
    def __init__(self, agent, grant_loader, factory=CommandProcess):
        self.agent = agent
        self.grant_loader = grant_loader
        self.factory = factory
        self.capacity = TaskPool(agent.state / "task-slots")
        self.folder = agent.state / "computer-jobs"
        reject_links(self.folder)
        self.folder.mkdir(exist_ok=True)
        self.rows = {}
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.thread = threading.Thread(
            target=self._watch, daemon=True, name="UDB computer jobs"
        )
        self.thread.start()

    def start(self, program, arguments, cwd, timeout_seconds, grant):
        if (
            not isinstance(program, str)
            or not program
            or "\0" in program
            or not isinstance(arguments, list)
            or len(arguments) > 128
            or any(not isinstance(a, str) or "\0" in a for a in arguments)
            or sum(len(a) for a in arguments) > 24000
        ):
            raise PolicyError(
                "Program and at most 128 bounded string arguments are required"
            )
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
            raise PolicyError("Command timeout must be 1..3600 seconds")
        target = (
            Path(program)
            if Path(program).is_absolute()
            else Path(shutil.which(program) or "")
        )
        executable_path(target)
        self.agent.check_protected(target)
        folder = Path(cwd)
        if not folder.is_absolute() or not folder.is_dir():
            raise PolicyError("Provide an existing absolute working directory")
        reject_links(folder)
        with self.lock:
            if self.done.is_set():
                raise PolicyError(
                    "Computer job manager is closed; no process was started"
                )
            if any(r["status"] == "cleanup_failed" for r in self.rows.values()):
                raise PolicyError(
                    "A previous owned Job could not be closed; stop/restart the agent before new commands"
                )
            if (
                sum(r["status"] == "running" for r in self.rows.values())
                >= MAX_CONCURRENT
            ):
                raise PolicyError(
                    "Maximum 10 computer commands; reuse or stop a job first"
                )
            while len(self.rows) >= 30:
                old = next(
                    (k for k, v in self.rows.items() if v["status"] != "running"), None
                )
                if old is None:
                    break
                self.rows.pop(old)["output"].unlink(missing_ok=True)
            jid = uuid.uuid4().hex
            output = self.folder / (jid + ".log")
            slot = self.capacity.claim()
            try:
                proc = self.factory([str(target), *arguments], folder, output)
            except BaseException:
                slot.close()
                raise
            self.rows[jid] = dict(
                process=proc,
                capacity=slot,
                output=output,
                status="running",
                exit_code=None,
                created=time.time(),
                until=time.monotonic() + timeout_seconds,
                grant=grant,
            )
            self.agent.audit(
                "computer_command_started", dict(job_id=jid, program=target.name)
            )
            return dict(job_id=jid, status="running", replay_safe=False)

    def _finish(self, row, status, code=None):
        row["status"] = status
        row["exit_code"] = code
        try:
            row["process"].close()
            if row.get("capacity") is not None:
                row["capacity"].close()
        except Exception:
            row["status"] = "cleanup_failed"

    def _watch(self):
        while not self.done.wait(0.15):
            with self.lock:
                active = [r for r in self.rows.values() if r["status"] == "running"]
                if not active:
                    continue
                try:
                    token = self.grant_loader()
                except Exception:
                    token = None
                for row in active:
                    try:
                        if token is None or token != row["grant"]:
                            self._finish(row, "revoked")
                        elif time.monotonic() > row["until"]:
                            self._finish(row, "timed_out")
                        elif row["output"].stat().st_size > MAX_OUTPUT:
                            self._finish(row, "output_limit")
                        else:
                            code = row["process"].poll()
                            if code is not None:
                                self._finish(row, "exited", code)
                    except Exception:
                        self._finish(row, "failed")

    def process(self, action, job_id="", offset=0):
        with self.lock:
            if action == "list":
                return dict(
                    jobs=[
                        dict(job_id=k, status=r["status"], exit_code=r["exit_code"])
                        for k, r in self.rows.items()
                    ]
                )
            if job_id not in self.rows:
                raise PolicyError(
                    "Unknown job; jobs are not replayed or reattached after agent restart"
                )
            row = self.rows[job_id]
            if action == "stop":
                if row["status"] in ("running", "cleanup_failed"):
                    self._finish(row, "stopped")
                return dict(job_id=job_id, status=row["status"])
            if (
                action != "read"
                or not isinstance(offset, int)
                or not 0 <= offset <= MAX_OUTPUT
            ):
                raise PolicyError(
                    "Process actions: list, read, stop; output offset 0..4 MiB"
                )
            with row["output"].open("rb") as stream:
                stream.seek(offset)
                data = stream.read(min(65536, MAX_OUTPUT - offset))
            return dict(
                job_id=job_id,
                status=row["status"],
                exit_code=row["exit_code"],
                output=data.decode("utf-8", errors="replace"),
                offset=offset,
                next_offset=offset + len(data),
                encoding="utf-8 with replacement",
                replay_safe=False,
            )

    def close(self):
        self.done.set()
        self.thread.join(timeout=5)
        with self.lock:
            for row in self.rows.values():
                if row["status"] in ("running", "cleanup_failed"):
                    self._finish(row, "agent_stopped")
            failed = [
                jid
                for jid, row in self.rows.items()
                if row["status"] == "cleanup_failed"
            ]
        if failed:
            raise PolicyError(
                "Owned job cleanup incomplete; retry close: " + ", ".join(failed)
            )
