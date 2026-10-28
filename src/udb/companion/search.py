"""Isolated background search orchestration and bounded worker.

SearchMixin only depends on the host's files/new-job interface; the worker
runs in its own bounded process and never inherits host-side UI permissions.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from udb.companion.processes import ManagedProcess
from udb.launcher import worker_command
from udb.pipe_io import write_all


class SearchMixin:
    def search(self, **args):
        root = self.files.path(args.get("path", "."))
        if not root.is_dir():
            raise ValueError("Search path must be a directory")
        query = args.get("text", "")
        pattern = args.get("pattern", "*")
        if (
            not isinstance(query, str)
            or len(query) > 500
            or not isinstance(pattern, str)
            or len(pattern) > 200
        ):
            raise ValueError("Bounded search text/pattern required")
        seconds = int(args.get("timeout_seconds", 30))
        if not 1 <= seconds <= 120:
            raise ValueError("Search timeout 1..120 seconds")
        config = dict(
            root=str(self.files.root),
            path=str(root),
            text=query,
            pattern=pattern,
            regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("case_sensitive", False)),
            search_type=args.get("search_type", "content"),
        )
        if config["search_type"] not in ("files", "content"):
            raise ValueError("Search type files or content")

        def run(task):
            proc = ManagedProcess(worker_command("search"), str(self.files.root), 256)
            task.worker = proc
            deadline = time.monotonic() + seconds

            def timeout():
                while proc.poll() is None:
                    if task.cancel.wait(0.05) or time.monotonic() > deadline:
                        proc.terminate()
                        return

            timer = threading.Thread(target=timeout, daemon=True)
            timer.start()
            try:
                write_all(proc.stdin, (json.dumps(config) + "\n").encode())
                proc.stdin.close()
                result = {}
                for line in iter(lambda: proc.stdout.readline(65537), b""):
                    task.check()
                    if len(line) > 65536:
                        raise ValueError("Search output frame exceeded limit")
                    row = json.loads(line)
                    with task.lock:
                        if row["type"] == "match":
                            if len(task.rows) >= 2000:
                                raise ValueError("Search result cap exceeded")
                            task.rows.append(row["value"])
                            task._checkpoint_version += 1
                        elif row["type"] == "progress":
                            task.progress = row["value"]
                            task._checkpoint_version += 1
                        elif row["type"] == "done":
                            result = row["value"]
                        elif row["type"] == "error":
                            raise ValueError(row["value"])
                task.check()
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Regex/search budget exhausted; partial results retained"
                    )
                if proc.wait(timeout=3) != 0 or not result:
                    raise RuntimeError("Search worker failed; partial results retained")
                return result
            finally:
                proc.close()
                task.worker = None
                for stream in (proc.stdin, proc.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

        return self.new(
            "search", run, recovery=dict(tool="search_start", arguments=args)
        )


def search_worker():
    from udb.companion.files import ScopedFiles

    config = json.loads(sys.stdin.buffer.readline(65536))
    files = ScopedFiles(config["root"])
    root = files.path(config["path"])
    query = config["text"]
    folded_query = query.casefold()
    flags = 0 if config["case_sensitive"] else re.IGNORECASE
    rx = None
    scanned = matches = 0
    truncated = False

    def send(kind, value):
        print(json.dumps(dict(type=kind, value=value), ensure_ascii=True), flush=True)

    try:
        rx = re.compile(query, flags) if config["regex"] else None
        for folder, dirs, names in os.walk(root, followlinks=False):

            def allowed(p):
                try:
                    files.path(str(p))
                    return True
                except (OSError, ValueError):
                    return False

            dirs[:] = [n for n in dirs if allowed(Path(folder) / n)]
            for name in names:
                scanned += 1
                if scanned > 20000:
                    truncated = True
                    break
                if scanned % 100 == 0:
                    send("progress", dict(files_scanned=scanned, matches=matches))
                if not fnmatch.fnmatch(name, config["pattern"]):
                    continue
                p = Path(folder) / name
                if not allowed(p) or not p.is_file():
                    continue
                if config["search_type"] == "files":
                    found = not query or (
                        bool(rx.search(name))
                        if rx
                        else (
                            query in name
                            if config["case_sensitive"]
                            else folded_query in name.casefold()
                        )
                    )
                    values = (
                        [dict(path=str(p.relative_to(files.root)))] if found else []
                    )
                else:
                    if p.stat().st_size > 64 * 1024 * 1024:
                        continue
                    values = []
                    try:
                        with p.open("r", encoding="utf-8", errors="strict") as f:
                            lineno = 0
                            while True:
                                line = f.readline(8193)
                                if not line:
                                    break
                                lineno += 1
                                if len(line) > 8192 and not line.endswith("\n"):
                                    while line and not line.endswith("\n"):
                                        line = f.readline(8193)
                                    continue
                                found = (
                                    bool(rx.search(line))
                                    if rx
                                    else (
                                        query in line
                                        if config["case_sensitive"]
                                        else folded_query in line.casefold()
                                    )
                                )
                                if found:
                                    send(
                                        "match",
                                        dict(
                                            path=str(p.relative_to(files.root)),
                                            line=lineno,
                                            text=line.rstrip()[:500],
                                        ),
                                    )
                                    matches += 1
                                    if matches >= 2000:
                                        break
                    except (OSError, UnicodeError):
                        continue
                for value in values:
                    send("match", value)
                    matches += 1
                if matches >= 2000:
                    truncated = True
                    break
            if truncated:
                break
        send("done", dict(files_scanned=scanned, matches=matches, truncated=truncated))
    except Exception as exc:
        send("error", type(exc).__name__ + ": " + str(exc))
        raise SystemExit(1) from exc
