"""Repeatable synthetic runtime costs; no user files, processes or grants used."""

import collections
import io
import json
import statistics
import tempfile
import threading
import time
import timeit
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from udb import activity, headless, host_service
from udb.companion import terminal
from udb.computer_files import MAX_FILE, Files
from udb.core import Agent


def file_costs():
    """Measure bounded computer-file operations on disposable local fixtures."""
    with tempfile.TemporaryDirectory(prefix="udb-file-benchmark-") as folder:
        root = Path(folder)
        files = Files(Agent(root / "owner"))
        source = root / "source.bin"
        with source.open("wb") as stream:
            for _ in range(MAX_FILE // 1048576):
                stream.write(b"x" * 1048576)
        expected = files.digest(source)
        metrics = {"input_bytes": MAX_FILE, "read_bytes": 200}
        for name, operation in (
            ("digest", lambda: files.digest(source)),
            (
                "read",
                lambda: files.dispatch("read", str(source), limit=200),
            ),
            (
                "copy",
                lambda: files.dispatch(
                    "copy",
                    str(source),
                    destination=str(root / "copy.bin"),
                    expected_sha256=expected,
                ),
            ),
        ):
            tracemalloc.start()
            try:
                started = time.perf_counter()
                result = operation()
                metrics[name + "_seconds"] = time.perf_counter() - started
                metrics[name + "_peak_bytes"] = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            if name == "digest":
                assert result == expected
        assert (root / "copy.bin").read_bytes() == source.read_bytes()
        return metrics


def idle_host_costs():
    clock, waits, cleanups = [0.0], [], []

    class Stop:
        def is_set(self):
            return clock[0] >= 9

        def wait(self, duration):
            waits.append(duration)
            clock[0] += duration

    with tempfile.TemporaryDirectory(prefix="udb-host-benchmark-") as folder:
        service = host_service.HostService.__new__(host_service.HostService)
        service.folder = Path(folder)
        (service.folder / "inbox").mkdir()
        (service.folder / "replies").mkdir()
        service.stop_event = Stop()
        service.maintenance = threading.Event()
        glob = Path.glob

        def counted(path, pattern, *args, **kwargs):
            if path.name == "replies":
                cleanups.append(clock[0])
            return glob(path, pattern, *args, **kwargs)

        with (
            patch.object(
                host_service,
                "time",
                SimpleNamespace(
                    monotonic=lambda: clock[0], time=lambda: clock[0] + 1000000
                ),
            ),
            patch.object(Path, "glob", counted),
        ):
            service._work()
    return dict(
        simulated_seconds=9,
        mailbox_polls=len(waits),
        reply_scans=len(cleanups),
        maximum_poll_seconds=max(waits),
    )


def main():
    chunks = collections.deque((i * 4096, bytes([i % 256]) * 4096) for i in range(256))
    copy_tail = getattr(
        terminal, "retained_tail", lambda rows: b"".join(c for _, c in rows)[-65536:]
    )
    assert copy_tail(chunks) == b"".join(c for _, c in chunks)[-65536:]
    times = timeit.repeat(lambda: copy_tail(chunks), number=2000, repeat=5)
    tracemalloc.start()
    copied = copy_tail(chunks)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    clock = [0.0]
    publications = []
    stop_checks = []

    class Event:
        def is_set(self):
            return clock[0] >= 9

        def wait(self, interval):
            clock[0] += interval

    controller = SimpleNamespace(
        start_host=lambda: None, close=lambda: None, agent=None
    )
    with (
        patch.object(headless, "time", SimpleNamespace(monotonic=lambda: clock[0])),
        patch.object(headless, "publish", lambda *a: publications.append(clock[0])),
        patch.object(
            headless, "stop_requested", lambda *a: stop_checks.append(clock[0]) or False
        ),
    ):
        controller.agent = SimpleNamespace(state=Path("synthetic-state"))
        headless.run_loop(controller, "fixture", Event(), connect=False)
    with tempfile.TemporaryDirectory(prefix="udb-runtime-benchmark-") as folder:
        activity.Activity(folder).emit("call", "synthetic")
        connects = []
        connect = activity.sqlite3.connect
        ticks = []

        def counted(*args, **kwargs):
            connects.append(True)
            return connect(*args, **kwargs)

        def tick(_):
            if len(ticks) == 50:
                raise KeyboardInterrupt
            ticks.append(True)

        started = time.perf_counter()
        with (
            patch.object(activity.sqlite3, "connect", counted),
            patch.object(activity.time, "sleep", tick),
        ):
            assert activity.watch(folder, tail=0, output=io.StringIO()) == 0
        polling_seconds = time.perf_counter() - started
    print(
        json.dumps(
            dict(
                scope="synthetic local hot paths; excludes network/model/browser latency",
                checkpoint_input_bytes=1048576,
                checkpoint_output_bytes=len(copied),
                checkpoint_peak_bytes=peak,
                checkpoint_median_microseconds=statistics.median(times) / 2000 * 1e6,
                idle_log_polls=50,
                idle_log_connections=len(connects),
                idle_log_seconds=polling_seconds,
                simulated_seconds=9,
                heartbeat_writes=len(publications) - 1,
                local_stop_checks=len(stop_checks),
                computer_files=file_costs(),
                idle_host=idle_host_costs(),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
