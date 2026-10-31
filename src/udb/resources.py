"""Low-cost Windows totals and a conservative admission governor.

CPU is a delta, never the misleading first GetSystemTimes reading. This is
admission control, not a promise against every short CPU/memory spike.
"""

from __future__ import annotations

import ctypes
import math
import os
import time

from .core import PolicyError

LIMITS = dict(
    cpu_pause=85,
    cpu_resume=70,
    ram_pause=85,
    ram_resume=75,
    min_free_mib=1536,
    resume_free_mib=2048,
    max_sessions=2,
    max_tabs=4,
    max_browser_processes=24,
    browser_memory_mib=1536,
    commit_pause=90,
    commit_resume=80,
    min_commit_mib=512,
    light_min_free_mib=256,
    light_min_commit_mib=128,
)


class WindowsSampler:
    def __init__(self):
        self.previous = None

    def sample(self):
        if os.name != "nt":
            raise PolicyError("Windows resource sampler unavailable")
        from ctypes import wintypes as w

        class Memory(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [
                (n, ctypes.c_ulonglong)
                for n in (
                    "total",
                    "available",
                    "commit_limit",
                    "commit_available",
                    "virtual",
                    "virtual_available",
                    "extended",
                )
            ]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        mem = Memory()
        mem.length = ctypes.sizeof(mem)
        if not k.GlobalMemoryStatusEx(ctypes.byref(mem)):
            raise OSError(ctypes.get_last_error(), "Memory counters unavailable")
        idle, kernel, user = (
            ctypes.c_ulonglong(),
            ctypes.c_ulonglong(),
            ctypes.c_ulonglong(),
        )
        if not k.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError(ctypes.get_last_error(), "CPU counters unavailable")
        current = (idle.value, kernel.value + user.value)
        cpu = None
        if self.previous:
            di, dt = current[0] - self.previous[0], current[1] - self.previous[1]
            if dt > 0:
                cpu = round(max(0, min(100, 100 * (dt - di) / dt)), 1)
        self.previous = current

        # GlobalMemoryStatusEx page-file fields may be process/job limited.
        # PERFORMANCE_INFORMATION gives system-wide commit values in pages.
        class Performance(ctypes.Structure):
            _fields_ = (
                [("cb", w.DWORD)]
                + [
                    (n, ctypes.c_size_t)
                    for n in (
                        "CommitTotal",
                        "CommitLimit",
                        "CommitPeak",
                        "PhysicalTotal",
                        "PhysicalAvailable",
                        "SystemCache",
                        "KernelTotal",
                        "KernelPaged",
                        "KernelNonpaged",
                        "PageSize",
                    )
                ]
                + [(n, w.DWORD) for n in ("HandleCount", "ProcessCount", "ThreadCount")]
            )

        perf = Performance()
        perf.cb = ctypes.sizeof(perf)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(Performance), w.DWORD]
        psapi.GetPerformanceInfo.restype = w.BOOL
        if (
            not psapi.GetPerformanceInfo(ctypes.byref(perf), perf.cb)
            or not perf.CommitLimit
            or not perf.PageSize
        ):
            raise OSError(ctypes.get_last_error(), "System commit counters unavailable")
        commit_available = max(0, perf.CommitLimit - perf.CommitTotal) * perf.PageSize
        return dict(
            at=time.time(),
            cpu_percent=cpu,
            ram_percent=mem.load,
            total_mib=round(mem.total / 1048576),
            available_mib=round(mem.available / 1048576),
            commit_percent=round(100 * perf.CommitTotal / perf.CommitLimit, 1),
            commit_available_mib=commit_available / 1048576,
            commit_limit_mib=perf.CommitLimit * perf.PageSize / 1048576,
            commit_source="GetPerformanceInfo",
        )


def fresh(status):
    at = status.get("at")
    return (
        isinstance(at, (float, int))
        and not isinstance(at, bool)
        and math.isfinite(at)
        and -2 <= time.time() - at <= 6
    )


def light_capacity_reason(status, reserve_mib=64):
    """Bounded file/UI inspection needs memory, not a cool CPU history."""
    if not fresh(status):
        return "monitor_unavailable"
    free, commit = status.get("available_mib"), status.get("commit_available_mib")
    if any(
        not isinstance(x, (float, int)) or isinstance(x, bool) or not math.isfinite(x)
        for x in (free, commit)
    ):
        return "monitor_unavailable"
    if free - reserve_mib < LIMITS["light_min_free_mib"]:
        return "insufficient_physical_memory"
    if commit - reserve_mib < LIMITS["light_min_commit_mib"]:
        return "insufficient_commit_headroom"
    return ""


def require_light_capacity(status, reserve_mib=64):
    reason = light_capacity_reason(status, reserve_mib)
    if reason:
        raise PolicyError("Light operation refused: " + reason)


class Governor:
    def __init__(self):
        self.paused = False
        self.cool = 0
        self.reason = "warming_up"
        self.pressures = set()

    def update(self, sample):
        cpu = sample.get("cpu_percent")
        metrics = (
            ("ram_percent", 100),
            ("available_mib", None),
            ("commit_percent", 100),
        )
        # A corrupt or stale monitor reading must never authorize new work.
        # Keep hysteresis after telemetry returns rather than treating a
        # missing resource sample as proof that the machine recovered.
        invalid = not fresh(sample)
        for name, upper in metrics:
            value = sample.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (upper is not None and value > upper)
            ):
                invalid = True
        commit_free = sample.get("commit_available_mib")
        if commit_free is not None and (
            isinstance(commit_free, bool)
            or not isinstance(commit_free, (int, float))
            or not math.isfinite(commit_free)
            or commit_free < 0
        ):
            invalid = True
        if cpu is not None and (
            isinstance(cpu, bool)
            or not isinstance(cpu, (int, float))
            or not math.isfinite(cpu)
            or not 0 <= cpu <= 100
        ):
            invalid = True
        if invalid:
            self.paused = True
            self.cool = 0
            self.reason = "monitor_unavailable"
            return self.status(sample)
        if cpu is None:
            self.reason = "warming_up"
            return self.status(sample)
        high = {
            "cpu": cpu >= LIMITS["cpu_pause"],
            "ram": sample["ram_percent"] >= LIMITS["ram_pause"],
            "free_memory": sample["available_mib"] < LIMITS["min_free_mib"],
            "commit": sample.get("commit_percent", 0) >= LIMITS["commit_pause"],
        }
        current = {cause for cause, active in high.items() if active}
        recovered = {
            "cpu": cpu < LIMITS["cpu_resume"],
            "ram": sample["ram_percent"] < LIMITS["ram_resume"],
            "free_memory": sample["available_mib"] >= LIMITS["resume_free_mib"],
            "commit": sample.get("commit_percent", 0) < LIMITS["commit_resume"],
        }
        if current:
            self.pressures.update(current)
            self.paused = True
            self.cool = 0
            self.reason = "machine_busy"
        elif self.paused:
            # Only causes which actually paused us need hysteresis recovery.
            cool = all(recovered[cause] for cause in self.pressures)
            self.cool = self.cool + 1 if cool else 0
            if self.cool >= 3:
                self.paused = False
                self.reason = ""
                self.pressures.clear()
            else:
                self.reason = "waiting_for_recovery"
        else:
            self.reason = ""
        return self.status(sample)

    def status(self, sample):
        light_reason = light_capacity_reason(sample)
        return dict(
            sample,
            paused=self.paused,
            admission_ready=not self.paused and not self.reason,
            reason=self.reason,
            pressure_causes=sorted(self.pressures),
            light_operations_ready=not light_reason,
            light_reason=light_reason,
            limits=dict(LIMITS),
        )


def require_capacity(status, reserve_mib=256):
    if not fresh(status) or not status.get("admission_ready"):
        raise PolicyError(
            "Machine busy or resource monitor unavailable; retry after 6 seconds"
        )
    available = status.get("available_mib")
    if (
        isinstance(available, bool)
        or not isinstance(available, (int, float))
        or not math.isfinite(available)
        or available < 0
    ):
        raise PolicyError("Resource monitor returned invalid free RAM")
    if available - reserve_mib < LIMITS["min_free_mib"]:
        raise PolicyError("Insufficient free RAM; no new task was opened")
    commit = status.get("commit_available_mib")
    if commit is not None:
        if (
            isinstance(commit, bool)
            or not isinstance(commit, (int, float))
            or not math.isfinite(commit)
            or commit < 0
        ):
            raise PolicyError("Resource monitor returned invalid commit headroom")
        if commit - reserve_mib < LIMITS["min_commit_mib"]:
            raise PolicyError(
                "Insufficient system commit headroom; no new task was opened"
            )


def guard_new_heavy_task(agent, reserve_mib=512):
    if os.name != "nt":
        return
    from .host_client import status

    state = status(agent)
    resource = state["resources"]
    if not state.get("worker_ready"):
        # Legacy local clients still get admission checks before a launch.
        # The current native GUI supplies the continuous monitor instead.
        sampler = WindowsSampler()
        sampler.sample()
        time.sleep(0.2)
        resource = Governor().update(sampler.sample())
    require_capacity(resource, reserve_mib)
